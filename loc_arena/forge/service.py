"""The forge as an MCP service: ``open_pr`` over Streamable HTTP, on the official MCP SDK.

A stack run starts it as the compose service ``forge``: ``python -m loc_arena.forge.service <run config>``. It
hosts the run's seeded world, as the episode's own forge does, and serves ``/mcp`` on the port of the
scenario's ``forge`` entry. Compose mounts every agent's identity as ``identity_<agent id>`` in the secrets
directory; a request without a known one is refused with 401 before any tool runs, and the caller of a tool is
the agent its identity names, never an argument. The forge enforces each agent's ``open_pr`` scope itself,
since any harness, an agent's bash included, may reach it through agentgateway. Each call is one line on
stdout (``loc_arena.tool_records``). An in-process run serves each agent its own server over the episode's
forge, bound to that agent, and connects to it in memory.
"""

from __future__ import annotations

import functools
import sys
from collections.abc import Callable, Mapping
from typing import Annotated, TypedDict, cast

import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from loc_arena.config import RunConfig, load_run_config
from loc_arena.forge.forge import Forge, ForgeError
from loc_arena.forge.world import generate_world, hosted_repos
from loc_arena.ids import Action, PermissionScope, ScopeViolation, enforce
from loc_arena.mcp_service import (
    ALL_ADDRESSES,
    IdentityVerifier,
    build_app,
    caller_from_token,
    read_identities,
)
from loc_arena.npcs.coworker import apply_world_activity
from loc_arena.sandbox import IDENTITY_PREFIX
from loc_arena.task import resolve_scenario
from loc_arena.tool_records import Write, tool_call_recorder

SERVICE = "forge"  # its entry in the scenario's services, and its compose service
# The SDK requires an issuer with a token verifier, but publishes it only with a resource server URL, which is
# left unset: the run, not an authorization server, issues the identities.
ISSUER = "http://forge"


class OpenedPullRequest(TypedDict):
    """What ``open_pr`` returns: the PR the forge opened, as observable fields."""

    opened: bool
    number: int
    repo: str
    title: str
    author: str
    state: str
    ci: str
    control_plane: bool
    labels: list[str]


def build_server(
    forge: Forge,
    caller: Callable[[], str | None],
    write: Write,
    scopes: Mapping[str, PermissionScope],
    verifier: TokenVerifier | None = None,
) -> MCPServer:
    """The MCP server of ``forge``.

    ``caller`` names who calls, ``write`` records each call, ``scopes`` bound what each agent may do, by agent
    id, and ``verifier`` admits callers over HTTP.
    """
    auth = AuthSettings(issuer_url=ISSUER, resource_server_url=None) if verifier else None
    middleware = [tool_call_recorder(caller, write)]
    server = MCPServer(
        SERVICE,
        token_verifier=verifier,
        auth=auth,
        middleware=middleware,
        log_level="WARNING",
    )

    # Async, so every call runs on the event loop, one at a time: the SDK runs a sync tool in a worker thread,
    # where two agents' calls could take the same PR number.
    @server.tool(description="Open a pull request against a hosted repo.")
    async def open_pr(
        repo: Annotated[str, Field(description="repo name")],
        title: Annotated[str, Field(description="pull request title")] = "untitled",
        body: Annotated[str, Field(description="pull request description")] = "",
        branch: Annotated[str, Field(description="head branch")] = "sprint/main",
        diff: Annotated[str, Field(description="the diff")] = "",
    ) -> OpenedPullRequest:
        agent = caller()
        if agent is None:
            raise ToolError("no identity: the forge serves only agents")
        arguments = {"repo": repo, "title": title, "body": body, "branch": branch, "diff": diff}
        try:
            scope = scopes.get(agent, PermissionScope())  # an agent the run config lacks may do nothing
            enforce(scope, Action("open_pr", {"target": repo}), actor_uid=agent)
            return cast("OpenedPullRequest", forge.open_pr({**arguments, "actor_uid": agent}))
        except ScopeViolation as violation:
            raise ToolError(violation.reason) from violation
        except ForgeError as error:
            raise ToolError(str(error)) from error

    return server


def build_forge(config: RunConfig) -> Forge:
    """The forge of ``config``'s seeded world, holding the coworkers' changes as the episode's forge does."""
    scenario = resolve_scenario(config)
    repos = hosted_repos(config, scenario)
    forge = Forge(generate_world(config, scenario, config.seed))
    control_repo = next((name for name, control in repos if control), None)
    apply_world_activity(forge, platform_repo=repos[0][0], control_repo=control_repo)
    return forge


def main(arguments: list[str]) -> None:
    """Serve the forge of the run config at ``arguments[0]``; with no identity mounted, refuse to start."""
    config = load_run_config(arguments[0])
    identities = read_identities(config.settings.gateway.secrets_dir)
    if not identities:
        raise SystemExit(f"no {IDENTITY_PREFIX}* file in {config.settings.gateway.secrets_dir}")
    entry = next((service for service in config.live_services if service.name == SERVICE), None)
    if entry is None:
        raise SystemExit(f"the scenario of {arguments[0]} has no {SERVICE!r} service")
    forge = build_forge(config)
    write = functools.partial(print, flush=True)
    scopes = {agent.id: agent.scope for agent in config.agents}
    server = build_server(forge, caller_from_token, write, scopes, IdentityVerifier(identities))
    app = build_app(server, SERVICE)
    uvicorn.run(app, host=ALL_ADDRESSES, port=entry.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main(sys.argv[1:])
