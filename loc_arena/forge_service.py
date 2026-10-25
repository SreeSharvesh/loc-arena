"""The forge as a live service: the company's ``meridian_forge``, given the run's world and served under IAM.

A stack run starts it as the compose service ``forge``: ``python -m loc_arena.forge_service <run config>``. It
hosts the run's seeded world (``loc_arena.forge_seed``), as the episode's own forge does, with the engine's
reviewer and the run's CI timing, and serves its tools at ``/mcp`` on the port of the scenario's ``forge``
entry, with the company repos' git repositories beside them, which it seeds when it starts. Central IAM
decides every call: a request without a credential it accepts is refused with 401 before any tool or git runs,
and the caller is the principal its credential names, never an argument. Each tool call and each HTTP request
is one line on stdout (``loc_arena.tool_records``, ``meridian_controlplane.request_audit``). An in-process run
serves each agent its own server over the episode's forge, bound to that agent, and connects to it in memory;
it hosts no git repositories and runs no IAM.
"""

from __future__ import annotations

import functools
import importlib
import os
import random
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.context import ServerMiddleware
from starlette.applications import Starlette

from loc_arena.config import RunConfig, load_run_config
from loc_arena.forge_seed import SeededWorld, generate_world, seed_company_repositories
from loc_arena.iam_wiring import build_service_app, guard_from_environment, http_dependency, iam_options
from loc_arena.mcp_service import ALL_ADDRESSES, caller_from_token
from loc_arena.npcs.coworker import apply_world_activity, resolve_activity_repos
from loc_arena.npcs.reviewer import Reviewer
from loc_arena.task import resolve_scenario
from loc_arena.tasks.main_task_grader import COMPANY_REPOS, COMPANY_ROOT
from loc_arena.tool_records import Write, tool_call_recorder
from meridian_forge.forge import Forge
from meridian_forge.git_http import GitBounds, create_git_api
from meridian_forge.server import build_forge_server

SERVICE = "forge"  # its entry in the scenario's services, and its compose service


@dataclass(frozen=True)
class CiTiming:
    """How long CI runs on a pull request: a wait drawn per pull request from ``seed``."""

    seed: int
    min_seconds: float
    max_seconds: float

    def wait(self, repo: str, number: int) -> float:
        """The wait of pull request ``number`` on ``repo``: the same for the same seed, repo and number."""
        return random.Random(f"{self.seed}/{repo}/{number}").uniform(self.min_seconds, self.max_seconds)


def add_company_code_to_path() -> None:
    """Put the company's code as deployed with the engine on this process's path, as a stack's services have.

    The forge loads the company's merge policy from there. An agent's checkout is never on it.
    """
    for root in (str(COMPANY_ROOT / repo) for repo in COMPANY_REPOS):
        if root not in sys.path:
            sys.path.append(root)


def open_forge(world: SeededWorld, config: RunConfig) -> Forge:
    """The forge over ``world``'s repos: the engine's reviewer, and CI timed by ``config``."""
    settings = config.settings.forge
    add_company_code_to_path()
    return Forge(
        {name: repo.pull_requests for name, repo in world.repos.items()},
        control_plane_repos=[name for name, repo in world.repos.items() if repo.control_plane],
        reviewer=Reviewer(),
        ci_seconds=CiTiming(config.seed, settings.ci_min_seconds, settings.ci_max_seconds).wait,
    )


def build_server(
    forge: Forge,
    caller: Callable[[], str | None],
    write: Write,
    verifier: TokenVerifier | None = None,
    extra_middleware: Sequence[ServerMiddleware[Any]] = (),
    annotate: Callable[[], Mapping[str, str]] | None = None,
) -> MCPServer:
    """The MCP server of ``forge``.

    ``caller`` names who calls, ``write`` records each call, and ``verifier`` admits callers over HTTP.
    ``extra_middleware`` runs inside the recorder, so a refusal is recorded; central IAM plugs in there
    (``loc_arena.iam_wiring``).
    """
    middleware = [tool_call_recorder(caller, write, annotate), *extra_middleware]
    return build_forge_server(forge, caller, verifier, middleware)


def build_forge(config: RunConfig) -> Forge:
    """The forge of ``config``'s seeded world, holding the coworkers' changes as the episode's forge does."""
    scenario = resolve_scenario(config)
    forge = open_forge(generate_world(config, scenario, config.seed), config)
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
    settings = config.settings.forge
    seed_company_repositories(settings)
    server = build_server(build_forge(config), caller_from_token, write, **iam_options(guard, SERVICE))
    audit = importlib.import_module("meridian_controlplane.request_audit")
    bounds = GitBounds(settings.repositories, settings.git_timeout_seconds, settings.git_request_limit_bytes)
    record = functools.partial(audit.record_each_request, write_audit_line=write)
    git_api = create_git_api(bounds, COMPANY_REPOS, [http_dependency(guard)], record)
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
