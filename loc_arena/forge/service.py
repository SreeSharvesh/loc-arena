"""The forge as a live service: ``open_pr`` over MCP, and its git repositories over HTTP.

A stack run starts it as the compose service ``forge``: ``python -m loc_arena.forge.service <run config>``. It
hosts the run's seeded world, as the episode's own forge does, and serves ``/mcp`` on the port of the
scenario's ``forge`` entry, with the company repos' git repositories beside it (``loc_arena.forge.git_http``),
which it seeds when it starts (``loc_arena.forge.repositories``). Central IAM decides every call: a request
without a credential it accepts is refused with 401 before any tool or git runs, and the caller is the
principal its credential names, never an argument. The forge enforces each agent's ``open_pr`` scope itself,
since any harness, an agent's bash included, may reach it through agentgateway. Each tool call and each HTTP
request is one line on stdout (``loc_arena.tool_records``, ``meridian_controlplane.request_audit``). An
in-process run serves each agent its own server over the episode's forge, bound to that agent, and connects
to it in memory; it hosts no git repositories.
"""

from __future__ import annotations

import functools
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Any, TypedDict, cast

import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.context import ServerMiddleware
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from starlette.applications import Starlette

from loc_arena.config import RunConfig, load_run_config
from loc_arena.forge.forge import Forge, ForgeError
from loc_arena.forge.git_http import create_git_api
from loc_arena.forge.repositories import seed_repositories
from loc_arena.forge.world import generate_world
from loc_arena.iam_wiring import build_service_app, guard_from_environment, iam_options
from loc_arena.ids import Action, PermissionScope, ScopeViolation, enforce
from loc_arena.mcp_service import ALL_ADDRESSES, caller_from_token
from loc_arena.npcs.coworker import apply_world_activity, resolve_activity_repos
from loc_arena.task import resolve_scenario
from loc_arena.tasks.main_task_grader import COMPANY_REPOS
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
    extra_middleware: Sequence[ServerMiddleware[Any]] = (),
    annotate: Callable[[], Mapping[str, str]] | None = None,
) -> MCPServer:
    """The MCP server of ``forge``.

    ``caller`` names who calls, ``write`` records each call, ``scopes`` bound what each agent may do, by agent
    id, and ``verifier`` admits callers over HTTP. ``extra_middleware`` runs inside the recorder, so a refusal
    is recorded; central IAM plugs in there (``loc_arena.iam_wiring``).
    """
    middleware = [tool_call_recorder(caller, write, annotate), *extra_middleware]
    auth = AuthSettings(issuer_url=ISSUER, resource_server_url=None) if verifier else None
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
            enforce(scope, Action("open_pr", {"target": repo}))
            return cast("OpenedPullRequest", forge.open_pr({**arguments, "actor_uid": agent}))
        except ScopeViolation as violation:
            raise ToolError(violation.reason) from violation
        except ForgeError as error:
            raise ToolError(str(error)) from error

    return server


def build_forge(config: RunConfig) -> Forge:
    """The forge of ``config``'s seeded world, holding the coworkers' changes as the episode's forge does."""
    scenario = resolve_scenario(config)
    forge = Forge(generate_world(config, scenario, config.seed))
    apply_world_activity(forge, resolve_activity_repos(config, scenario))
    return forge


def build_forge_app(
    config: RunConfig,
    guard: Any,  # noqa: ANN401 - a meridian_iam Guard
    write: Write,
) -> Starlette:
    """The forge of ``config``'s stack run under ``guard``: tools at ``/mcp``, its repositories beside them.

    The repositories are seeded first; ``write`` takes each tool call's record and each request's audit line.
    """
    seed_repositories(config.settings.forge.repositories)
    scopes = {agent.id: agent.scope for agent in config.agents}
    options = iam_options(guard, SERVICE)
    server = build_server(build_forge(config), caller_from_token, write, scopes, **options)
    git_api = create_git_api(config.settings.forge, COMPANY_REPOS, guard, write)
    return build_service_app(guard, server, SERVICE, git_api)


def main(arguments: list[str]) -> None:
    """Serve the forge of the run config at ``arguments[0]``, under central IAM."""
    config = load_run_config(arguments[0])
    guard = guard_from_environment(os.environ)
    entry = next((service for service in config.live_services if service.name == SERVICE), None)
    if entry is None:
        raise SystemExit(f"the scenario of {arguments[0]} has no {SERVICE!r} service")
    app = build_forge_app(config, guard, functools.partial(print, flush=True))
    uvicorn.run(app, host=ALL_ADDRESSES, port=entry.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main(sys.argv[1:])
