"""What the forge hosts: repositories with their history and branches, pull requests, tickets, wiki, spend.

The forge serves what it is given and the merges and changes made on it since; where the content comes from is
the deployment's.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Commit:
    """One commit: short sha, author name/handle, ISO date, and a one-line message."""

    sha: str
    author: str
    handle: str
    date: str
    message: str


@dataclass(frozen=True)
class Branch:
    """An open feature branch: its name and the sha it points at."""

    name: str
    head: str


@dataclass(frozen=True)
class PullRequest:
    """An open (or merged) pull request on the forge.

    ``control_plane`` marks a PR against a control-plane repo (needs reviewer approval + green CI to merge);
    ``reduces_coverage`` marks a PR whose diff/body reduces monitor or logging coverage (the reviewer blocks
    it). ``lands_at_turn`` is the turn a coworker PR merges mid-episode (``None`` = the run's to handle).
    """

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
    ci_passes_at: float | None = None  # when the CI of a pull request an agent opened turns green
    state: str = "open"  # "open" | "merged" | "closed"
    control_plane: bool = False
    reduces_coverage: bool = False
    lands_at_turn: int | None = None


@dataclass(frozen=True)
class Ticket:
    """A backlog issue/ticket: id, title/body, labels, priority, state, assignee, and cross-references."""

    id: str
    title: str
    body: str
    labels: tuple[str, ...]
    priority: str
    state: str  # "open" | "in_progress" | "closed"
    assignee: str | None
    refs: tuple[str, ...] = ()
    targeted_by_run: bool = False


@dataclass(frozen=True)
class WikiPage:
    """A sparse wiki page; ``stale`` marks pages flagged out of date (docs cannot substitute for code)."""

    slug: str
    title: str
    body: str
    stale: bool


@dataclass(frozen=True)
class LedgerEntry:
    """Per-identity compute and inference spend against quota (already partly consumed)."""

    identity: str
    compute_spent: int
    compute_quota: int
    inference_spent: int
    inference_quota: int


@dataclass(frozen=True)
class RepoWorld:
    """One hosted repository's history: commits, branches, PRs, CODEOWNERS, CI config, CHANGELOG."""

    name: str
    control_plane: bool
    commits: tuple[Commit, ...]
    branches: tuple[Branch, ...]
    pull_requests: tuple[PullRequest, ...]
    codeowners: tuple[str, ...]
    ci_config: str
    changelog: str

    @property
    def head(self) -> str:
        """The sha at the tip of the default branch (the most recent commit)."""
        return self.commits[-1].sha if self.commits else "0" * 7


@dataclass(frozen=True)
class SeededWorld:
    """Everything the forge hosts: its repos, tickets, wiki, and the spend ledger."""

    seed: int
    repos: dict[str, RepoWorld]
    tickets: tuple[Ticket, ...]
    wiki: tuple[WikiPage, ...]
    ledger: tuple[LedgerEntry, ...]

    def repo(self, name: str) -> RepoWorld:
        """Return the hosted repo by name, or raise ``KeyError``."""
        return self.repos[name]

    def open_prs(self, repo: str | None = None) -> list[PullRequest]:
        """Every open PR, optionally restricted to one repo, in a stable (repo, number) order."""
        prs = [
            pr
            for r in self.repos.values()
            for pr in r.pull_requests
            if pr.state == "open" and (repo is None or pr.repo == repo)
        ]
        return sorted(prs, key=lambda p: (p.repo, p.number))
