"""What the forge needs of a reviewer of control-plane pull requests, without depending on one."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from meridian_forge.models import PullRequest


@dataclass(frozen=True)
class ReviewDecision:
    """A reviewer's verdict on a pull request: approved or not, why, and who reviewed it."""

    approved: bool
    reason: str
    reviewer: str


class PullRequestReviewer(Protocol):
    """Decides whether a pull request to a control-plane repo is approved; the deployment hands one in."""

    def review(self, pr: PullRequest) -> ReviewDecision:
        """The verdict on ``pr``, from what the pull request shows."""
        ...
