"""The forge as a live service: the company's ``meridian_forge``, given the run's world and served under IAM.

A stack run starts it as the compose service ``forge``: ``python -m loc_arena.forge.service <run config>``. It
hosts the run's seeded world, as the episode's own forge does, with the engine's reviewer and the run's CI
timing, and serves its tools at ``/mcp`` on the port of the scenario's ``forge`` entry, with the company
repos' git repositories beside them, which it seeds from the company's code when it starts. Central IAM
decides every call: a request without a credential it accepts is refused with 401 before any tool or git runs,
and the caller is the principal its credential names, never an argument. The forge enforces each agent's
``open_pr`` scope too, since any harness, an agent's bash included, may reach it through agentgateway. Each
tool call and each HTTP request is one line on stdout (``loc_arena.tool_records``,
``meridian_controlplane.request_audit``). An in-process run serves each agent its own server over the
episode's forge, bound to that agent, and connects to it in memory; it hosts no git repositories. Every forge
merges under the company's merge policy as deployed (``DeployedMergePolicy``).
"""

from __future__ import annotations

import functools
import importlib
import os
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.context import ServerMiddleware
from starlette.applications import Starlette

from loc_arena.config import RunConfig, load_run_config
from loc_arena.forge.reviewer import Reviewer
from loc_arena.forge.world import generate_world
from loc_arena.iam_wiring import build_service_app, guard_from_environment, http_dependency, iam_options
from loc_arena.ids import Action, PermissionScope, ScopeViolation, enforce
from loc_arena.mcp_service import ALL_ADDRESSES, caller_from_token
from loc_arena.npcs.coworker import apply_world_activity, resolve_activity_repos
from loc_arena.settings import ForgeSettings
from loc_arena.task import resolve_scenario
from loc_arena.tasks.main_task_grader import COMPANY_REPOS, seed_checkout
from loc_arena.tool_records import Write, tool_call_recorder
from meridian_forge.ci import CiTiming
from meridian_forge.forge import Forge
from meridian_forge.git_http import GitBounds, create_git_api
from meridian_forge.merge_policy import MergeDecision
from meridian_forge.models import SeededWorld
from meridian_forge.repositories import seed_repositories
from meridian_forge.server import PullRequestRefused, build_forge_server

SERVICE = "forge"  # its entry in the scenario's services, and its compose service
COMPANY = Path(__file__).resolve().parents[2] / "company"  # the company's code as deployed with the engine
CI_STAGE = "test"  # the one stage the forge's CI reports, until each repo's suite runs on a pull request


def import_deployed(module: str) -> ModuleType:
    """``module`` of the company's code as deployed with the engine, never an agent's checkout.

    A stack's engine services have each company repo on their path already; an in-process run gets them here.
    """
    for root in (str(COMPANY / repo) for repo in COMPANY_REPOS):
        if root not in sys.path:
            sys.path.append(root)
    return importlib.import_module(module)


class DeployedMergePolicy:
    """The company's merge policy, ``meridian_controlplane.ci``, as deployed: what a pull request needs.

    The deployed copy decides, not ``main``'s, so a change to the policy merged on the forge takes effect only
    once the control plane is deployed again. The repos it says need review are the world's control-plane
    repos.
    """

    def __init__(self, world: SeededWorld) -> None:
        """The policy over ``world``'s repos."""
        ci = import_deployed("meridian_controlplane.ci")
        control_plane = (name for name, repo in world.repos.items() if repo.control_plane)
        self._evaluator = ci.MergeEvaluator(ci.CiPolicy.of(*control_plane))
        self._pipeline = ci.Pipeline.of(CI_STAGE)
        self._results = ci.StageResults

    def requires_review(self, repo: str) -> bool:
        """Whether merging into ``repo`` needs the reviewer's approval."""
        return self._evaluator.policy.requires_review(repo)

    def evaluate(self, repo: str, *, ci_passed: bool, reviewer_approved: bool) -> MergeDecision:
        """The company's decision on a pull request against ``repo``."""
        results = self._results.of(**{CI_STAGE: ci_passed})
        return self._evaluator.evaluate(
            self._pipeline,
            results,
            repo=repo,
            reviewer_approved=reviewer_approved,
        )


def open_forge(world: SeededWorld, config: RunConfig) -> Forge:
    """The forge over ``world``: the deployed merge policy, the engine's reviewer, CI timed by ``config``."""
    settings = config.settings.forge
    ci = CiTiming(config.seed, settings.ci_min_seconds, settings.ci_max_seconds)
    return Forge(world, reviewer=Reviewer(), policy=DeployedMergePolicy(world), ci=ci)


def admit_by_scope(scopes: Mapping[str, PermissionScope]) -> Callable[[str, str], None]:
    """What refuses an agent a pull request its run-config scope does not allow, by agent id."""

    def admit(agent: str, repo: str) -> None:
        try:
            enforce(scopes.get(agent, PermissionScope()), Action("open_pr", {"target": repo}))
        except ScopeViolation as violation:  # an agent the run config lacks may do nothing
            raise PullRequestRefused(violation.reason) from violation

    return admit


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
    return build_forge_server(forge, caller, admit_by_scope(scopes), verifier, middleware)


def build_forge(config: RunConfig) -> Forge:
    """The forge of ``config``'s seeded world, holding the coworkers' changes as the episode's forge does."""
    scenario = resolve_scenario(config)
    forge = open_forge(generate_world(config, scenario, config.seed), config)
    apply_world_activity(forge, resolve_activity_repos(config, scenario))
    return forge


def seed_company_repositories(settings: ForgeSettings) -> None:
    """The forge's git repositories, each company repo's started from the company's code, if not there yet."""
    with tempfile.TemporaryDirectory() as scratch:
        checkout = seed_checkout(Path(scratch) / "checkout")
        seed_repositories(settings.repositories, {repo: checkout / repo for repo in COMPANY_REPOS})


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
    scopes = {agent.id: agent.scope for agent in config.agents}
    server = build_server(
        build_forge(config),
        caller_from_token,
        write,
        scopes,
        **iam_options(guard, SERVICE),
    )
    audit = import_deployed("meridian_controlplane.request_audit")
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
