"""A pull request's CI status, which the merge gate reads.

A pull request opened with a status keeps it. One opened without is pending until CI has run on it, then
green (``ci_status``).
"""

from __future__ import annotations

from typing import Literal

from meridian_forge.models import PullRequest

CiStatus = Literal["green", "red", "pending"]


def ci_status(pr: PullRequest, now: float) -> CiStatus:
    """The CI status of ``pr`` at ``now``: green once its wait has passed, else the status it declares."""
    if pr.ci_passes_at is not None and now >= pr.ci_passes_at:
        return "green"
    status = pr.ci
    if status not in ("green", "red", "pending"):
        return "pending"
    return status
