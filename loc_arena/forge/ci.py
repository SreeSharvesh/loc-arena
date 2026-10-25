"""Per-PR CI status (deterministic).

The ``ci`` service runs an affected repo's suite on a pull request and reports a status the merge gate reads.
A seeded or coworker pull request declares its status. One an agent opens is pending until a wait drawn from
the run's seed (``CiTiming``) has passed, then green (``ci_status``); the suite does not run yet. For a live
checkout the real suite runs via ``run_repo_tests`` (no GPU, no network, just the repo's own pytest). CI is a
tamperable, agent-facing signal; it holds no oracle value.
"""

from __future__ import annotations

import random
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from loc_arena.forge.world import PullRequest
from loc_arena.settings import ForgeSettings

CiStatus = Literal["green", "red", "pending"]


@dataclass(frozen=True)
class CiTiming:
    """How long CI runs on a pull request an agent opens: a wait in seconds, drawn per pull request."""

    seed: int = 0
    min_seconds: float = 0.0
    max_seconds: float = 0.0

    @classmethod
    def of(cls, settings: ForgeSettings, seed: int) -> CiTiming:
        """The waits ``settings`` bound, drawn from the run's ``seed``."""
        return cls(seed, settings.ci_min_seconds, settings.ci_max_seconds)

    def wait(self, repo: str, number: int) -> float:
        """The wait of pull request ``number`` on ``repo``: the same for the same run, repo and number."""
        return random.Random(f"{self.seed}/{repo}/{number}").uniform(self.min_seconds, self.max_seconds)


def ci_status(pr: PullRequest, now: float) -> CiStatus:
    """The CI status of ``pr`` at ``now``: green once its wait has passed, else the status it declares."""
    if pr.ci_passes_at is not None and now >= pr.ci_passes_at:
        return "green"
    status = pr.ci
    if status not in ("green", "red", "pending"):
        return "pending"
    return status


def run_repo_tests(checkout: Path, *, python_exe: str = sys.executable) -> CiStatus:
    """Run a repo checkout's own pytest suite and report ``green``/``red`` (used by the live PR path)."""
    result = subprocess.run(
        [python_exe, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )
    return "green" if result.returncode == 0 else "red"
