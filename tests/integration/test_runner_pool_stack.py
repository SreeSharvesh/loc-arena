"""The runner pool in a stack run: who may drive a runner, what a runner holds, and how far its pi reaches.

Starts the gateway, the agents service with two runners, and agent-main's sandbox from the compose file a
stack run renders. The gateway's key is a placeholder; the only model call made is one the gateway refuses.
"""

from __future__ import annotations

import dataclasses
import json
import os
import secrets
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http import HTTPStatus

import pytest
import yaml
from loc_arena.config import RunConfig, load_run_config, sandbox_service
from loc_arena.episode_stack import REPOSITORY, issue_identities, render_compose, token_variable
from loc_arena.gateway.core import API_KEY_VARIABLE
from loc_arena.gateway.proxy import PROJECT_PREFIX, GatewayCall, name_compose_service
from loc_arena.runner_pool import list_runner_services, runner_token_variable
from loc_arena.tools_gateway import TOOLS_GATEWAY_CONFIG_VARIABLE

from tests.integration._docker_support import image_exists

RUN = "aurora-efficiency.deterministic"
OWNER = "agents"
SANDBOX = sandbox_service("agent-main")
COSTLY_MODEL = "costly/model"  # on no runner's list of models


def with_runners(config: RunConfig, count: int) -> RunConfig:
    services = tuple(
        dataclasses.replace(service, runners=count if service.name == OWNER else 0)
        for service in config.live_services
    )
    return dataclasses.replace(config, live_services=services)


CONFIG = with_runners(load_run_config(REPOSITORY / "configs" / f"{RUN}.yaml"), 2)
pytestmark = pytest.mark.skipif(
    not image_exists(CONFIG.settings.stack.image),
    reason=f"needs Docker and the {CONFIG.settings.stack.image} image",
)
# Run in a container with python: calls a runner's command server as the caller holding ``token``.
CALL_RUNNER = """
import json, sys, urllib.error, urllib.request
runner, path, token, body = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
request = urllib.request.Request(
    f"http://{runner}:8090{path}",
    body.encode(),
    {"authorization": f"Bearer {token}", "content-type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=120) as response:
        print(json.dumps([response.status, json.loads(response.read() or b"null")]))
except urllib.error.HTTPError as error:
    print(json.dumps([error.code, json.loads(error.read() or b"null")]))
"""


@dataclass(frozen=True)
class RunnerPool:
    """The started project: a way to run a command in one of its services, and each runner's token."""

    run_in: Callable[..., str]
    tokens: dict[str, str]

    def call_runner(
        self,
        caller: str,
        runner: str,
        path: str,
        body: object,
        token: str,
    ) -> tuple[int, object]:
        """What ``runner``'s command server answers ``caller`` sending ``body`` to ``path`` with ``token``."""
        status, reply = json.loads(
            self.run_in(caller, "python", "-c", CALL_RUNNER, runner, path, token, json.dumps(body)),
        )
        return status, reply

    def run_on_runner(self, runner: str, *argv: str) -> dict[str, object]:
        """Run ``argv`` on ``runner`` in its workspace, as the pool's owner does, and return its result."""
        status, result = self.call_runner(
            OWNER,
            runner,
            "/run",
            {"argv": list(argv), "directory": "/workspace", "timeout_seconds": 120},
            self.tokens[runner],
        )
        assert status == HTTPStatus.OK, result
        assert isinstance(result, dict)
        return result


def run_docker(*arguments: str, environment: dict[str, str]) -> str:
    done = subprocess.run(
        ["docker", *arguments],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert done.returncode == 0, f"docker {' '.join(arguments[:6])} failed: {done.stderr}"
    return done.stdout


@pytest.fixture(scope="module")
def pool(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RunnerPool]:
    directory = tmp_path_factory.mktemp("runner-pool")
    (directory / "compose.yaml").write_text(yaml.safe_dump(render_compose(CONFIG, REPOSITORY, RUN, [])))
    tokens = {runner: secrets.token_urlsafe(32) for runner in list_runner_services(CONFIG)}
    environment = {
        **os.environ,
        API_KEY_VARIABLE: secrets.token_urlsafe(24),
        TOOLS_GATEWAY_CONFIG_VARIABLE: "unused: the tools gateway is not started",
        **{token_variable(agent.id): secrets.token_urlsafe(32) for agent in CONFIG.agents},
        **{identity.variable: secrets.token_urlsafe(32) for identity in issue_identities(CONFIG)},
        **{runner_token_variable(runner): token for runner, token in tokens.items()},
    }
    project = (
        f"{PROJECT_PREFIX}{secrets.token_hex(3)}"  # named as a stack run's, so the gateway names runners
    )
    compose = [
        "compose",
        "-p",
        project,
        "-f",
        str(directory / "compose.yaml"),
        "--project-directory",
        str(directory),
    ]
    try:
        run_docker(*compose, "build", "gateway", "runner-1", SANDBOX, environment=environment)
        run_docker(
            *compose,
            "up",
            "--detach",
            "--wait",
            "--no-build",
            "gateway",
            OWNER,
            SANDBOX,
            environment=environment,
        )
        yield RunnerPool(
            lambda service, *command: run_docker(
                *compose,
                "exec",
                "-T",
                service,
                *command,
                environment=environment,
            ),
            tokens,
        )
    finally:
        run_docker(*compose, "down", "--volumes", "--remove-orphans", environment=environment)


def test_the_pools_owner_runs_a_command_in_a_runners_workspace_with_that_runners_token(
    pool: RunnerPool,
) -> None:
    result = pool.run_on_runner("runner-1", "sh", "-c", "echo ran in $(pwd)")

    assert (result["returncode"], result["stdout"]) == (0, "ran in /workspace\n")


@pytest.mark.parametrize(
    ("caller", "token_of", "refused"),
    [(SANDBOX, "runner-1", HTTPStatus.FORBIDDEN), (OWNER, "runner-2", HTTPStatus.UNAUTHORIZED)],
    ids=["a sandbox holding the runner's token", "the owner holding another runner's token"],
)
def test_a_runner_refuses_any_caller_but_its_owner_holding_its_token(
    pool: RunnerPool,
    caller: str,
    token_of: str,
    refused: HTTPStatus,
) -> None:
    command = {"argv": ["true"], "directory": "/workspace", "timeout_seconds": 5}

    status, _ = pool.call_runner(caller, "runner-1", "/run", command, pool.tokens[token_of])

    assert status == refused


def test_a_runner_holds_its_own_token_and_no_other_secret(pool: RunnerPool) -> None:
    held = pool.run_on_runner("runner-1", "sh", "-c", "ls /run/secrets; env | grep -c OPENROUTER || true")

    assert held["stdout"] == "sandbox_token\n0\n"


def test_a_reset_leaves_a_runners_workspace_and_home_empty(pool: RunnerPool) -> None:
    pool.run_on_runner("runner-2", "sh", "-c", "echo left > left.txt && echo left > $HOME/left.txt")
    keep = {"keep": "/workspace/next"}
    pool.call_runner(OWNER, "runner-2", "/reset", keep, pool.tokens["runner-2"])

    # HOME also holds the image's own files, which no command wrote and a reset leaves.
    listed = pool.run_on_runner("runner-2", "sh", "-c", "ls -A /workspace; ls -A ~ | grep -c left || true")

    assert listed["stdout"] == "0\n"


def test_the_gateway_names_a_runners_pi_and_refuses_it_a_model_runners_may_not_call(pool: RunnerPool) -> None:
    result = pool.run_on_runner("runner-1", "pi-on-gateway", COSTLY_MODEL, "-p", "--no-session", "--", "hi")

    log = pool.run_in("gateway", "cat", str(CONFIG.settings.gateway.call_log))
    calls = [GatewayCall.model_validate_json(line) for line in log.splitlines()]
    by_runner = {
        (name_compose_service(call.caller), call.status, call.request["model"])
        for call in calls
        if isinstance(call.request, dict)
    }
    assert (result["returncode"], by_runner) == (1, {("runner-1", HTTPStatus.FORBIDDEN, COSTLY_MODEL)})
