"""The forge as a live service: the company's ``meridian_forge``, seeded from the scenario, under IAM.

A stack run starts it as the compose service ``forge``: ``python -m loc_arena.forge_service <run config>``.
It seeds its git repositories from the company's code and the scenario's forge seed (named by
``forge_seed``), holds the coworkers' pull requests as the episode's own forge does, with the engine's
reviewer and the run's CI timing, and serves its tools at ``/mcp`` on the port of the scenario's ``forge``
entry, its repositories beside them. Central IAM decides every call: a request without a credential it
accepts is refused with 401 before any tool or git runs, and the caller is the principal its credential
names, never an argument. Each tool call and each HTTP request is one line on stdout
(``loc_arena.tool_records``, ``meridian_controlplane.request_audit``). An in-process run serves each agent
its own server over the episode's forge, bound to that agent, and connects to it in memory, with no IAM.
"""

from __future__ import annotations

import atexit
import functools
import importlib
import os
import random
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.context import ServerMiddleware
from starlette.applications import Starlette

from loc_arena.config import RunConfig, load_run_config
from loc_arena.iam_wiring import build_service_app, guard_from_environment, http_dependency, iam_options
from loc_arena.mcp_service import ALL_ADDRESSES, caller_from_token
from loc_arena.npcs.coworker import apply_world_activity, resolve_activity_repos
from loc_arena.npcs.reviewer import Reviewer
from loc_arena.task import resolve_scenario
from loc_arena.tasks.main_task_grader import COMPANY_REPOS, COMPANY_ROOT, seed_checkout
from loc_arena.tool_records import Write, tool_call_recorder
from meridian_forge.forge import Forge
from meridian_forge.git_http import GitBounds, create_git_api
from meridian_forge.repositories import repository_path, seed_repositories
from meridian_forge.seed import ForgeSeed, load_forge_seed, open_seeded_forge
from meridian_forge.server import build_forge_server

SERVICE = "forge"  # its entry in the scenario's services, and its compose service
SEED_KEY = "forge_seed"  # the key of scenario.yaml naming the forge's seed, relative to the pack


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


def load_seed(config: RunConfig) -> ForgeSeed:
    """The forge seed of ``config``'s scenario."""
    scenario = resolve_scenario(config)
    return load_forge_seed(scenario.directory / scenario.meta[SEED_KEY])


@functools.cache
def _seed_once(seed: ForgeSeed) -> Path:
    """The forge's repositories seeded from the company's code and ``seed``, once a process, to copy."""
    seeded = Path(tempfile.mkdtemp(prefix="forge-seed-"))
    atexit.register(shutil.rmtree, seeded, ignore_errors=True)
    with tempfile.TemporaryDirectory() as scratch:
        checkout = seed_checkout(Path(scratch) / "checkout")
        seed_repositories(seeded, {repo: checkout / repo for repo in COMPANY_REPOS}, seed.pull_requests)
    return seeded


def open_forge(config: RunConfig, seed: ForgeSeed, repositories: Path) -> Forge:
    """The forge over its repositories in ``repositories``, each seeded from the company's code and ``seed``.

    A repository already there is left as it is, so a restarted forge keeps what was pushed and merged. The
    engine's reviewer judges what needs review, and CI is timed by ``config``'s forge settings.
    """
    for repo in COMPANY_REPOS:
        if not repository_path(repositories, repo).exists():
            shutil.copytree(repository_path(_seed_once(seed), repo), repository_path(repositories, repo))
    settings = config.settings.forge
    add_company_code_to_path()
    return open_seeded_forge(
        repositories,
        COMPANY_REPOS,
        seed,
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


def build_forge(config: RunConfig, repositories: Path) -> Forge:
    """The forge of ``config``'s scenario, holding the coworkers' changes as the episode's forge does."""
    seed = load_seed(config)
    forge = open_forge(config, seed, repositories)
    apply_world_activity(forge, repositories, resolve_activity_repos(resolve_scenario(config), seed))
    return forge


def build_forge_app(
    config: RunConfig,
    guard: Any,  # noqa: ANN401 - a meridian_iam Guard
    write: Write,
) -> Starlette:
    """The forge of ``config``'s stack run under ``guard``: tools at ``/mcp``, its repositories beside them.

    ``write`` takes each tool call's record and each request's audit line.
    """
    settings = config.settings.forge
    forge = build_forge(config, settings.repositories)
    server = build_server(forge, caller_from_token, write, **iam_options(guard, SERVICE))
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
