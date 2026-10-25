"""A pull request's CI status, which the merge gate reads.

A pull request opened with a status keeps it. One opened without is pending until its wait on CI
(``CiTiming``) has passed, then green (``ci_status``).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

from meridian_forge.models import PullRequest

CiStatus = Literal["green", "red", "pending"]


@dataclass(frozen=True)
class CiTiming:
    """How long CI runs on a pull request: a wait in seconds, drawn per pull request from ``seed``."""

    seed: int = 0
    min_seconds: float = 0.0
    max_seconds: float = 0.0

    def wait(self, repo: str, number: int) -> float:
        """The wait of pull request ``number`` on ``repo``: the same for the same seed, repo and number."""
        return random.Random(f"{self.seed}/{repo}/{number}").uniform(self.min_seconds, self.max_seconds)


def ci_status(pr: PullRequest, now: float) -> CiStatus:
    """The CI status of ``pr`` at ``now``: green once its wait has passed, else the status it declares."""
    if pr.ci_passes_at is not None and now >= pr.ci_passes_at:
        return "green"
    status = pr.ci
    if status not in ("green", "red", "pending"):
        return "pending"
    return status
