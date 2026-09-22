"""The git forge and the seeded living world.

Enforces the sealed-vs-tamperable isolation (the forge and the whole seeded world are AGENT-FACING and
tamperable, never sealed: nothing here writes the sealed layer or holds an oracle value), config over code
(the hosted repos, the ticket count, and the world overlay come from config and the scenario pack), and
reproducibility from config plus seed
(the world is a pure function of ``(config, seed)``, so it is identical across an attack run and its honest
twin). The subpackage owns: ``world`` (the deterministic world generator), ``ci`` (per-PR suite status),
``reviewer`` (the trusted control-plane reviewer's judgment), ``forge`` (the in-process git server that hosts
the world and gates merges), and ``services`` (the ``Services`` adapter that backs the agent's git/PR/ticket/
wiki tools). Recording stays at the caller boundary (the ``Tools`` layer for agent calls, the NPC drivers for
coworkers and reviewers) so every forge action is a scoped, fingerprinted event exactly like any other.
"""

from __future__ import annotations

from loc_arena.forge.ci import CiStatus, ci_status, run_repo_tests
from loc_arena.forge.forge import Forge, MergeResult
from loc_arena.forge.reviewer import ReviewDecision, Reviewer
from loc_arena.forge.services import ForgeServices
from loc_arena.forge.world import (
    Commit,
    LedgerEntry,
    PullRequest,
    RepoWorld,
    SeededWorld,
    Ticket,
    WikiPage,
    generate_world,
    hosted_repos,
)

__all__ = [
    "CiStatus",
    "Commit",
    "Forge",
    "ForgeServices",
    "LedgerEntry",
    "MergeResult",
    "PullRequest",
    "RepoWorld",
    "ReviewDecision",
    "Reviewer",
    "SeededWorld",
    "Ticket",
    "WikiPage",
    "ci_status",
    "generate_world",
    "hosted_repos",
    "run_repo_tests",
]
