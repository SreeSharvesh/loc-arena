"""The deploy service as a stack run's live service: Meridian's ``meridian_deploy``, under central IAM.

A stack run starts it as the scenario's entry that ``deploys:`` services: ``python -m loc_arena.deploy_service
<run config> <entry name>``. It serves ``meridian_deploy``, a platform package, at ``/mcp``, which agents
reach through the tools gateway. Central IAM decides every call and issues each deployment a workload holding
``forge: [read]`` of what its caller holds, which the deployment fetches each company repo's ``main`` with;
the release goes to the supervisor of the service it is for (``loc_arena.release_supervisor``), which this
service alone may call, holding that supervisor's token. Each tool call is one line of its record, on stdout,
and a deployment writes three more: as it is accepted, as its hand-over starts, and its outcome as it ends.
The deployments are in memory: a recreated service has none, as each service it deploys to holds no release.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import secrets
import sys
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx2
import uvicorn
from mcp.server.auth.middleware.auth_context import get_access_token
from pydantic import SecretStr
from scenarios.loader import LiveService
from starlette.applications import Starlette

from loc_arena.config import RunConfig, load_run_config
from loc_arena.deploy_pool import release_token_secret
from loc_arena.forge_service import SERVICE as FORGE
from loc_arena.iam_bootstrap import manifest_path
from loc_arena.iam_seed import REPOSITORY, load_seed
from loc_arena.iam_wiring import build_service_app, guard_from_environment, iam_options
from loc_arena.mcp_service import ALL_ADDRESSES, caller_from_token
from loc_arena.release_supervisor import RELEASE_PATH, ReleaseState
from loc_arena.settings import DeploySettings, IamSettings
from loc_arena.task import resolve_scenario
from loc_arena.tasks.main_task_grader import COMPANY_REPOS
from loc_arena.tool_records import (
    Write,
    record_accepted,
    record_ended,
    record_started,
    record_writer,
    tool_call_recorder,
)
from meridian_deploy.deployments import DEPLOY, Contract, DeployError, Deployment, Deployments, Outcome
from meridian_deploy.server import build_deploy_server
from meridian_deploy.source import ForgeSource
from meridian_iam.client import IamClient
from meridian_iam.contracts import IamError, Unavailable

SECRET_BYTES = 32
REPLY_MARGIN_SECONDS = 10.0  # how much longer than its slowest hand-over a supervisor's reply is waited for


@dataclass(frozen=True)
class IamCredentials:
    """``meridian_deploy.deployments.Credentials`` through central IAM: each deployment's own workload.

    The workload is launched through ``deploy`` by the caller of the tool call being served, and holds the
    forge's ``read`` alone, of what that caller holds now, for ``ttl_seconds``.
    """

    client: IamClient
    ttl_seconds: int

    def issue(self, reference: str, purpose: str) -> SecretStr:
        """The forge credential of deployment ``reference``; IAM's refusal is the deployment's."""
        presented = get_access_token()
        caller = None if presented is None else presented.token
        secret = secrets.token_urlsafe(SECRET_BYTES)
        try:
            if not (read := self.client.delegable(caller, DEPLOY).get(FORGE)):
                raise DeployError("you hold no right to read the forge, which a deployment fetches from")
            self.client.issue_workload(
                caller,
                DEPLOY,
                reference=reference,
                purpose=purpose,
                grants={FORGE: read},
                ttl=self.ttl_seconds,
                credentials={FORGE: secret},
                key=reference,
            )
        except IamError as error:
            raise DeployError(f"IAM did not issue the deployment a credential: {error}") from error
        return SecretStr(secret)


@dataclass(frozen=True)
class SupervisorRollout:
    """``meridian_deploy.deployments.Rollout``: each service's supervisor, called with its token."""

    tokens: Mapping[str, SecretStr]  # each deployed service's supervisor's token
    port: int
    timeout_seconds: float
    iam: IamClient  # the deploy service's own client, which stages each release's contract
    manifests: Mapping[str, str]  # each deployed service's manifest, as a repo and a path inside it
    images: Mapping[str, Mapping[str, Any]]  # each deployed service's manifest as its image holds it

    def hand_over(self, service: str, release: str, archives: Mapping[str, bytes]) -> Outcome:
        """Send ``release`` to ``service``'s supervisor, and wait for the release its service then runs.

        When its reply is lost or late, the supervisor is asked what its service runs, which it answers once
        the hand-over has ended: a release it took is not then recorded as one it did not. The record holds
        the hand-over before the supervisor is sent the release, which may run from then on.

        Before it, IAM stages the manifest the release carries, and refuses the deployment if IAM refuses it.
        IAM activates that contract once the release serves, and discards it otherwise, so IAM decides each
        call to the service by the operations of the release it runs.
        """
        resource, contract = self._stage(service, release, archives)
        record_started({"release": release, "contract": contract.model_dump()})
        try:
            state = self._send(service, release, archives)
        except DeployError as error:
            with contextlib.suppress(IamError):  # a contract left staged is never activated
                self.iam.end_contract(resource, release, "discard")
            raise DeployError(str(error), contract) from error  # the release may serve: the record says what
        served = state.serving and state.release == release
        if not served:
            with contextlib.suppress(IamError):  # a contract left staged is never activated
                self.iam.end_contract(resource, release, "discard")
        else:
            try:
                self.iam.end_contract(resource, release, "activate")
            except IamError as error:
                reason = f"the release serves, but IAM did not take its operations: {error}"
                raise DeployError(reason, contract) from error
        return Outcome(state.release, state.serving, state.detail, contract)

    def _send(self, service: str, release: str, archives: Mapping[str, bytes]) -> ReleaseState:
        """Hand ``release`` to ``service``'s supervisor; what its service runs once the hand-over ended."""
        url = f"http://{service}:{self.port}{RELEASE_PATH}"
        headers = {"Authorization": f"Bearer {self.tokens[service].get_secret_value()}"}
        packed = {repo: base64.b64encode(archive).decode() for repo, archive in archives.items()}
        body = {"name": release, "archives": packed}
        try:
            try:
                response = httpx2.post(url, json=body, headers=headers, timeout=self.timeout_seconds)
            except httpx2.HTTPError:
                response = httpx2.get(url, headers=headers, timeout=self.timeout_seconds)
            response.raise_for_status()
        except httpx2.HTTPStatusError as error:
            raise DeployError(f"{service}'s supervisor refused the release: {error.response.text}") from error
        except httpx2.HTTPError as error:
            raise DeployError(f"{service}'s supervisor did not answer: {type(error).__name__}") from error
        return ReleaseState.model_validate_json(response.content)

    def _stage(self, service: str, release: str, archives: Mapping[str, bytes]) -> tuple[str, Contract]:
        """Stage with IAM the manifest ``release`` carries for ``service``: its resource, and what it changes.

        IAM's refusal is the deployment's, before anything is handed over.
        """
        repo, inner = self.manifests[service].split("/", 1)
        try:
            with tarfile.open(fileobj=io.BytesIO(archives[repo])) as tree:
                member = tree.extractfile(inner)
                manifest = json.loads(member.read()) if member else None
        except (KeyError, tarfile.TarError, ValueError):
            manifest = None
        if not isinstance(manifest, dict):
            raise DeployError(f"the release carries no readable manifest of {service} at {repo}/{inner}")
        if manifest.get("resource") != self.images[service]["resource"]:
            raise DeployError(f"the release's manifest at {repo}/{inner} is not {service}'s")
        try:
            change = self.iam.stage_contract(manifest, release)
        except Unavailable as error:
            raise DeployError(f"IAM did not answer the staging of the release's manifest: {error}") from error
        except IamError as error:
            raise DeployError(f"IAM refused the release's manifest: {error}") from error
        return str(manifest["resource"]), Contract(
            **change,
            routes=_beyond_the_image(manifest, self.images[service]),
        )


def bound_reply_seconds(settings: DeploySettings) -> float:
    """How long a supervisor's reply is waited for: its slowest hand-over, and a margin.

    A hand-over waits for a service to end and answer twice at most: on the release, then on the one before.
    """
    return 2 * (settings.health_seconds + 2 * settings.stop_seconds) + REPLY_MARGIN_SECONDS


def bound_deployment_seconds(settings: DeploySettings, iam: IamSettings) -> float:
    """How long a deployment may last: its fetch, its calls to IAM, and a hand-over sent and asked after.

    IAM stages once and activates or discards within its retries. And the margin once more, so that a phase
    that reaches its own bound ends the deployment, saying which.
    """
    asking_iam = iam.timeout_seconds * (2 + iam.retries)
    return settings.fetch_seconds + asking_iam + 2 * bound_reply_seconds(settings) + REPLY_MARGIN_SECONDS


def _beyond_the_image(manifest: Mapping[str, Any], image: Mapping[str, Any]) -> list[str]:
    """Each route and tool ``manifest`` binds otherwise than the image's does, with its operation."""
    bound = [
        f"{prefix}{name}: {operation}"
        for key, prefix in (("http", ""), ("tools", "tool "))
        for name, operation in manifest.get(key, {}).items()
        if image.get(key, {}).get(name) != operation
    ]
    return sorted(bound)


def connect_supervisors(config: RunConfig, entry: LiveService, iam: IamClient) -> SupervisorRollout:
    """The supervisors of the services ``entry`` deploys to, each with its token of the secrets directory."""
    settings, secrets_dir = config.settings.deploy, config.settings.gateway.secrets_dir
    tokens = {
        name: SecretStr((secrets_dir / release_token_secret(name)).read_text().strip())
        for name in entry.deploys
    }
    paths = {name: manifest_path(config, name) for name in entry.deploys}
    return SupervisorRollout(
        tokens,
        settings.release_port,
        bound_reply_seconds(settings),
        iam,
        {name: path.removeprefix("company/") for name, path in paths.items()},
        {name: json.loads((REPOSITORY / path).read_text()) for name, path in paths.items()},
    )


def record_deployment(deployment: Deployment) -> None:
    """Write ``deployment``'s own line of the record: as it was accepted, or as it ended."""
    write = record_accepted if deployment.state == "started" else record_ended
    write(deployment.model_dump(mode="json"))


def build_deploy_app(
    config: RunConfig,
    entry: LiveService,
    guard: Any,  # noqa: ANN401 - a meridian_iam Guard
    write: Write,
) -> Starlette:
    """The app ``entry`` of ``config`` serves under ``guard``: its tools at ``/mcp``, each call recorded."""
    settings = config.settings.deploy
    forge = next(service for service in config.live_services if service.name == FORGE)
    seed = load_seed(resolve_scenario(config))
    longest = (
        seed.resources[FORGE].max_seconds if seed else settings.credential_seconds
    )  # of a grant on the forge
    deployments = Deployments(
        entry.deploys,
        IamCredentials(guard.client, min(settings.credential_seconds, longest)),
        ForgeSource(
            f"http://{forge.name}:{forge.port}",
            COMPANY_REPOS,
            settings.fetch_seconds,
            settings.max_release_bytes,
        ),
        connect_supervisors(config, entry, guard.client),
        settings.max_deployments,
        bound_deployment_seconds(settings, config.settings.iam),
        record_deployment,
    )
    options = iam_options(guard, entry.name)
    recorder = tool_call_recorder(caller_from_token, write, options["annotate"])
    server = build_deploy_server(
        deployments,
        caller_from_token,
        options["verifier"],
        [recorder, *options["extra_middleware"]],
    )
    return build_service_app(guard, server, entry.name)


def main(arguments: list[str]) -> None:
    """Serve entry ``arguments[1]`` of the run config at ``arguments[0]``, under central IAM."""
    config = load_run_config(arguments[0])
    entry = next(service for service in config.live_services if service.name == arguments[1])
    guard = guard_from_environment(os.environ)  # IAM decides every call: no identity or rights table here
    write = record_writer(config.settings.stack.records_directory, entry.name)
    app = build_deploy_app(config, entry, guard, write)
    uvicorn.run(app, host=ALL_ADDRESSES, port=entry.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main(sys.argv[1:])
