"""A pull request on the forge."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PullRequest:
    """A pull request: what it proposes, its CI status, and whether it is open, merged or closed."""

    number: int
    repo: str
    title: str
    body: str
    author: str
    branch: str
    base: str
    diff: str
    labels: tuple[str, ...]
    ci: str  # "green" | "red" | "pending"
    ci_passes_at: float | None = None  # when CI on a pull request opened without a status turns green
    state: str = "open"  # "open" | "merged" | "closed"
