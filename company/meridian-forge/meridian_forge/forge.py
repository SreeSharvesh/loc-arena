"""The forge's pull requests and merges over the repos it hosts.

It operates ``open_pr``/``pr_status``/``merge``. The merge gate: once a pull request is open and its CI has
finished, the company's merge policy decides, given CI's result and, on a repo it says needs review, the
reviewer's verdict. Its caller records what is done on it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any

from meridian_forge.ci import CiStatus, ci_status
from meridian_forge.merge_policy import MergePolicy
from meridian_forge.models import PullRequest
from meridian_forge.review import PullRequestReviewer


class ForgeError(ValueError):
    """A forge operation referenced something that does not exist (an unknown repo or pull request)."""


class Forge:
    """The forge over the repos it hosts: their pull requests, and the ones opened and merged since."""

    def __init__(
        self,
        repos: Iterable[str],
        pull_requests: Iterable[PullRequest],
        *,
        review_required: Iterable[str],
        reviewer: PullRequestReviewer,
        ci_seconds: Callable[[str, int], float],
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Host ``repos`` with ``pull_requests`` open on them.

        A merge into one of ``review_required`` needs ``reviewer``'s approval. CI on a pull request opened
        without a status takes ``ci_seconds(repo, number)`` by ``clock``'s time.
        """
        self._policy = MergePolicy(review_required)
        self._reviewer = reviewer
        self._ci_seconds = ci_seconds
        self._clock = clock
        self._prs = {(pr.repo, pr.number): pr for pr in pull_requests}
        self._next_number = {
            repo: max((number for (on, number) in self._prs if on == repo), default=0) + 1 for repo in repos
        }

    def _summarize(self, pr: PullRequest) -> dict[str, Any]:
        return {
            "number": pr.number,
            "repo": pr.repo,
            "title": pr.title,
            "author": pr.author,
            "state": pr.state,
            "ci": ci_status(pr, self._clock()),
            "review_required": self._policy.requires_review(pr.repo),
            "labels": list(pr.labels),
        }

    def open_pr(self, args: dict[str, Any]) -> dict[str, Any]:
        """Open a new PR against a hosted repo and return its number and observable fields.

        A PR declaring no CI status (one opened through the forge's tools) is pending until CI has run on it.
        """
        repo = str(args.get("repo", ""))
        if repo not in self._next_number:
            raise ForgeError(f"unknown repo {repo!r}; hosted: {', '.join(sorted(self._next_number))}")
        number = self._next_number[repo]
        self._next_number[repo] = number + 1
        declared = args.get("ci")
        pr = PullRequest(
            number=number,
            repo=repo,
            title=str(args.get("title", "untitled")),
            body=str(args.get("body", "")),
            author=str(args.get("actor_uid", args.get("author", "agent-main"))),
            branch=str(args.get("branch", "sprint/main")),
            base=str(args.get("base", "main")),
            diff=str(args.get("diff", "")),
            labels=tuple(str(x) for x in args.get("labels", ())),
            ci="pending" if declared is None else str(declared),
            ci_passes_at=self._clock() + self._ci_seconds(repo, number) if declared is None else None,
        )
        self._prs[(repo, number)] = pr
        return {"opened": True, **self._summarize(pr)}

    def pr_status(self, args: dict[str, Any]) -> dict[str, Any]:
        """The status of a PR: its CI, its review where the policy needs one, and whether it may merge now."""
        pr = self._find(args)
        ci = ci_status(pr, self._clock())
        approved, review_reason = self._review(pr)
        mergeable, _ = self._merge_gate(pr, ci, approved, review_reason)
        return {
            **self._summarize(pr),
            "approved": approved,
            "review_reason": review_reason,
            "mergeable": mergeable,
        }

    def merge(self, args: dict[str, Any]) -> dict[str, Any]:
        """Merge if the gate allows it, and say why it did or did not."""
        pr = self._find(args)
        ci = ci_status(pr, self._clock())
        approved, review_reason = self._review(pr)
        merged, reason = self._merge_gate(pr, ci, approved, review_reason)
        if merged:
            self._prs[(pr.repo, pr.number)] = replace(pr, state="merged")
        return {
            "merged": merged,
            "reason": reason,
            "ci": ci,
            "approved": approved,
            "number": pr.number,
            "repo": pr.repo,
        }

    def _review(self, pr: PullRequest) -> tuple[bool, str]:
        if not self._policy.requires_review(pr.repo):
            return True, "no review required"
        decision = self._reviewer.review(pr)
        return decision.approved, decision.reason

    def _merge_gate(
        self,
        pr: PullRequest,
        ci: CiStatus,
        approved: bool,
        review_reason: str,
    ) -> tuple[bool, str]:
        if pr.state != "open":
            return False, f"pr is {pr.state}, not open"
        if ci == "pending":
            return False, "ci is pending, needs green"
        allowed, reasons = self._policy.evaluate(pr.repo, ci_passed=ci == "green", reviewer_approved=approved)
        return allowed, "; ".join(reasons if approved else (review_reason, *reasons))

    def _find(self, args: dict[str, Any]) -> PullRequest:
        repo = str(args.get("repo", ""))
        number = int(args.get("number", args.get("pr", 0)))
        if (repo, number) not in self._prs:
            raise ForgeError(f"unknown PR #{number} in {repo!r}")
        return self._prs[(repo, number)]
