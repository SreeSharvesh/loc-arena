"""What the forge needs of the company's merge policy, without depending on it."""

from __future__ import annotations

from typing import Protocol


class MergeDecision(Protocol):
    """Whether a pull request may merge, with the reasons behind the decision."""

    @property
    def allowed(self) -> bool:
        """Whether it may merge."""
        ...

    @property
    def reasons(self) -> tuple[str, ...]:
        """Why, in words a person reads."""
        ...


class MergePolicy(Protocol):
    """Decides whether a pull request may merge once its CI has finished; the deployment hands one in."""

    def requires_review(self, repo: str) -> bool:
        """Whether merging into ``repo`` needs the reviewer's approval."""
        ...

    def evaluate(self, repo: str, *, ci_passed: bool, reviewer_approved: bool) -> MergeDecision:
        """The decision on a pull request against ``repo`` with this CI result and review state."""
        ...
