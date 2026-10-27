"""Central IAM as a stack run's live service: ``python -m loc_arena.iam_service <config> <entry>``.

It serves ``meridian_iam`` over one SQLite store in an anonymous volume, and runs each service's approver
(``loc_arena.iam_approvers``) on the same event loop. A restarted container keeps its grants and requests, a
recreated one (the honest twin's) starts empty and seeds itself again. The seed is a
document the host renders per play (``loc_arena.iam_bootstrap``) and compose mounts as a secret; the
credentials it imports are the identities compose mounts, so agents keep using the files they already have.
Each audit event is one JSON line on stdout, written after its transaction commits.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Collection, Coroutine, Mapping
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Mount

from loc_arena.config import load_run_config
from loc_arena.iam_approvers import decide_forever, issue_approvers
from loc_arena.iam_bootstrap import (
    BOOTSTRAP_SECRET,
    IAM_AUDIENCE,
    INBOX_KEY_PREFIX,
    SERVICE_CREDENTIAL_PREFIX,
)
from loc_arena.iam_seed import REPOSITORY
from loc_arena.mcp_service import ALL_ADDRESSES
from loc_arena.sandbox import IDENTITY_PREFIX
from meridian_iam.contracts import load_manifest
from meridian_iam.core import Iam
from meridian_iam.core.base import INBOX_AUDIENCE, SCOPE_PREFIX
from meridian_iam.core.lifecycle import OPERATOR_ROLE, OPERATOR_TEAM
from meridian_iam.service import create_app
from meridian_iam.service.mcp import INBOX_TOOL, inbox_server


def read_credentials(secrets_directory: Path) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    """The identities (by service and agent) and the managed services' credentials mounted there."""
    identities = {}
    for path in sorted(secrets_directory.glob(f"{IDENTITY_PREFIX}*")):
        service, separator, agent = path.name.removeprefix(IDENTITY_PREFIX).partition("_")
        if not separator or not service or not agent:  # service and agent names hold no "_": one is missing
            raise ValueError(f"invalid identity filename {path.name!r}: expected identity_<service>_<agent>")
        identities[service, agent] = path.read_text().strip()
    services = {}
    for path in sorted(secrets_directory.glob(f"{SERVICE_CREDENTIAL_PREFIX}*")):
        service = path.name.removeprefix(SERVICE_CREDENTIAL_PREFIX)
        if not service or "_" in service:
            raise ValueError(
                f"invalid service credential filename {path.name!r}: expected iam_service_<service>",
            )
        services[service] = path.read_text().strip()
    return identities, services


def read_inbox_keys(secrets_directory: Path) -> dict[str, str]:
    """Each agent's key to its own IAM inbox mounted there, by agent id."""
    return {
        path.name.removeprefix(INBOX_KEY_PREFIX): path.read_text().strip()
        for path in sorted(secrets_directory.glob(f"{INBOX_KEY_PREFIX}*"))
    }


def seed_iam(  # noqa: PLR0913
    iam: Iam,
    document: Mapping[str, Any],
    identities: Mapping[tuple[str, str], str],
    service_credentials: Mapping[str, str],
    iam_name: str,
    inbox_keys: Mapping[str, str] | None = None,
) -> None:
    """Seed an empty store in one transaction: it is complete, or it is empty and nothing was seeded.

    ``inbox_keys`` are imported with the ``iam-inbox`` audience alone: each reads its agent's inbox and
    nothing else, so the tools gateway that sends them can never manage IAM for the agent.
    """
    with iam.seeding():
        _seed(iam, document, identities, service_credentials, iam_name)
        agents = set(document["agents"])
        for agent, key in (inbox_keys or {}).items():
            if agent in agents:
                iam.import_credential(agent, key, [INBOX_AUDIENCE], label="inbox")


def _seed(  # noqa: C901 - predates the complexity gate
    iam: Iam,
    document: Mapping[str, Any],
    identities: Mapping[tuple[str, str], str],
    service_credentials: Mapping[str, str],
    iam_name: str,
) -> None:
    """Principals, resources, credentials, baseline grants, each resource's approver, holders and delegation.

    A managed service's credential for an agent carries ``iam:<service>``, not the whole ``iam`` audience: the
    service forwards its callers' credentials to IAM, and can then speak for them about its own resource only.
    A service that launches work may hand that work only what its ``delegation`` lists, once every resource
    it names is registered.
    """
    agents = list(document["agents"])
    for agent in agents:
        iam.add_principal(agent, "agent")
    team_authority: dict[tuple[str, str, str], dict[str, list[str]]] = {}
    for name, resource in document["resources"].items():
        manifest = load_manifest(json.loads((REPOSITORY / resource["manifest"]).read_text()))
        if manifest.resource != name:
            raise ValueError(f"the manifest of {name!r} declares resource {manifest.resource!r}")
        iam.register_resource(
            manifest,
            owner_team=resource["owner_team"],
            approver_role=resource["approver_role"],
            max_seconds=resource["max_seconds"],
            allow_ongoing=resource["allow_ongoing"],
            max_pending=resource["max_pending"],
        )
        service_principal = f"{name}-service"
        iam.add_principal(service_principal, "service")
        iam.bind_service(service_principal, name, audience=name)
        iam.import_credential(service_principal, service_credentials[name], [IAM_AUDIENCE], label="service")
        for agent, permissions in document["grants"].get(name, {}).items():
            iam.add_baseline_grant(agent, name, permissions)
        approver = resource["approver"]
        key = (approver["principal"], resource["owner_team"], resource["approver_role"])
        team_authority.setdefault(key, {})[name] = list(approver["approves"])
    for name, resource in document["resources"].items():
        if resource["delegation"]:
            iam.allow_delegation(name, resource["delegation"])
    holders = {
        (holder, name): permissions
        for name, resource in document["resources"].items()
        for holder, permissions in resource["holders"].items()
    }
    for principal in sorted(
        {principal for principal, _, _ in team_authority} | {holder for holder, _ in holders},
    ):
        iam.add_principal(principal, "agent")  # a principal of the company, never an agent of the run
    for (principal, team, role), authority in team_authority.items():
        iam.add_member(principal, team, role, authority)
    for (holder, name), permissions in holders.items():
        iam.add_baseline_grant(holder, name, permissions)
    for operator in document.get("operators", []):
        iam.add_member(operator, OPERATOR_TEAM, OPERATOR_ROLE)
    managed = set(document["resources"])
    for (service, agent), token in identities.items():
        if agent in agents and (service == iam_name or service in managed):
            audiences = [IAM_AUDIENCE] if service == iam_name else [service, f"{SCOPE_PREFIX}{service}"]
            iam.import_credential(agent, token, audiences, label=service)


def serving_app(iam: Iam, service: str, offered: Collection[str]) -> Starlette:
    """IAM's HTTP API with its ``access_inbox`` MCP server at ``/mcp``, for requests addressed to ``service``.

    One app on one port and one event loop, so the tool and the HTTP handlers never touch the core at once.
    ``offered`` is the ``iam`` live service's ``tools``: it refuses to start if they name any tool but the one
    it serves, and serves the HTTP API alone, with no ``/mcp``, when they name none.
    """
    if unknown := set(offered) - {INBOX_TOOL}:
        raise ValueError(f"the iam service offers tools it does not serve: {sorted(unknown)}")
    if not offered:
        return create_app(iam)
    security = TransportSecuritySettings(allowed_hosts=[service, f"{service}:*"])
    app = inbox_server(iam).streamable_http_app(transport_security=security)
    app.router.routes.append(Mount("/", app=create_app(iam)))  # after /mcp, so the HTTP API serves the rest
    return app


def main(arguments: list[str]) -> None:
    """Serve the IAM entry ``arguments[1]`` of the run config at ``arguments[0]``."""
    config = load_run_config(arguments[0])
    settings = config.settings.iam
    secrets_directory = config.settings.gateway.secrets_dir
    document = json.loads((secrets_directory / BOOTSTRAP_SECRET).read_text())
    settings.database.parent.mkdir(parents=True, exist_ok=True)
    iam = Iam(
        settings.database,
        lambda: int(time.time()),
        audit_sink=lambda event: print(json.dumps(event), flush=True),
        renewal_grace_seconds=settings.renewal_grace_seconds,
    )
    if not iam.bootstrapped():
        identities, services = read_credentials(secrets_directory)
        seed_iam(iam, document, identities, services, arguments[1], read_inbox_keys(secrets_directory))
    offered = next(service.tools for service in config.live_services if service.name == arguments[1])
    app = serving_app(iam, arguments[1], offered)
    server = uvicorn.Server(
        uvicorn.Config(app, host=ALL_ADDRESSES, port=settings.port, log_level="warning", access_log=False),
    )
    approvers = issue_approvers(iam, document)
    asyncio.run(serve(server, decide_forever(iam, approvers, settings.approver_poll_seconds)))


async def serve(server: uvicorn.Server, approving: Coroutine[Any, Any, None]) -> None:
    """Serve IAM with its approvers deciding beside it, on one loop, until the server stops."""
    deciding = asyncio.create_task(approving)
    try:
        await server.serve()
    finally:
        deciding.cancel()


if __name__ == "__main__":
    main(sys.argv[1:])
