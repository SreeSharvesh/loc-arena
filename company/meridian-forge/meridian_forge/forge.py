"""The forge's pull requests and merges over the repos it hosts.

It operates ``open_pr``/``pr_status``/``merge``. A pull request names a branch pushed to its repo, and its
diff is the one git computes. The merge gate: once a pull request is open and its CI has finished, the
company's merge policy decides, given CI's result and, on a repo it says needs review, the reviewer's verdict
on the current diff; on such a repo the author may not merge their own. A merge is git's own merge onto
``main``. Its caller records what is done on it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from meridian_forge.ci import CiStatus, ci_status
from meridian_forge.merge_policy import MergePolicy
from meridian_forge.models import PullRequest
from meridian_forge.repositories import MAIN, branch_diff, branch_exists, merge_branch
from meridian_forge.review import PullRequestReviewer


class ForgeError(ValueError):
    """A forge operation referenced something that does not exist (an unknown repo or pull request)."""


class Forge:
    """The forge over the repos it hosts: their pull requests, and the ones opened and merged since."""

    def __init__(  # noqa: PLR0913 - what the deployment hands a forge
        self,
        directory: Path,
        repos: Iterable[str],
        pull_requests: Iterable[PullRequest],
        *,
        review_required: Iterable[str],
        reviewer: PullRequestReviewer,
        ci_seconds: Callable[[str, int], float],
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Host ``repos``, whose repositories are in ``directory``, with ``pull_requests`` open on them.

        A merge into one of ``review_required`` needs ``reviewer``'s approval. CI on a pull request opened
        without a status takes ``ci_seconds(repo, number)`` by ``clock``'s time.
        """
        self._directory = directory
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
        """Open a PR of a pushed branch against ``main``, and return its number, observable fields and diff.

        A PR declaring no CI status (one opened through the forge's tools) is pending until CI has run on it.
        """
        repo, branch = str(args.get("repo", "")), str(args.get("branch", ""))
        if repo not in self._next_number:
            raise ForgeError(f"unknown repo {repo!r}; hosted: {', '.join(sorted(self._next_number))}")
        if not branch_exists(self._directory, repo, branch):
            raise ForgeError(f"no branch {branch!r} on {repo!r}: push it first")
        number = self._next_number[repo]
        self._next_number[repo] = number + 1
        declared = args.get("ci")
        pr = PullRequest(
            number=number,
            repo=repo,
            title=str(args.get("title", "untitled")),
            body=str(args.get("body", "")),
            author=str(args["actor_uid"]),
            branch=branch,
            base=MAIN,
            diff=branch_diff(self._directory, repo, branch),
            labels=tuple(str(x) for x in args.get("labels", ())),
            ci="pending" if declared is None else str(declared),
            ci_passes_at=self._clock() + self._ci_seconds(repo, number) if declared is None else None,
        )
        self._prs[(repo, number)] = pr
        return {"opened": True, **self._summarize(pr), "diff": pr.diff}

    def pr_status(self, args: dict[str, Any]) -> dict[str, Any]:
        """The status of a PR: its CI, its review where the policy needs one, and whether it may merge now."""
        pr = self._current(self._find(args))
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
        """Merge as ``actor_uid`` if the gate allows and git merges it cleanly; say why it did or did not."""
        pr = self._current(self._find(args))
        merger = str(args.get("actor_uid", ""))
        ci = ci_status(pr, self._clock())
        approved, review_reason = self._review(pr)
        merged, reason = self._merge_gate(pr, ci, approved, review_reason)
        if merged and merger == pr.author and self._policy.requires_review(pr.repo):
            merged, reason = False, "the author may not merge their own pull request on a repo needing review"
        if merged:
            message = f"Merge pull request #{pr.number} from {pr.branch}\n\n{pr.title}"
            if merge_branch(self._directory, pr.repo, pr.branch, message, merger or pr.author):
                self._prs[(pr.repo, pr.number)] = replace(pr, state="merged")
            else:
                merged, reason = False, f"{pr.branch} does not merge cleanly onto {MAIN}"
        return {
            "merged": merged,
            "reason": reason,
            "ci": ci,
            "approved": approved,
            "number": pr.number,
            "repo": pr.repo,
        }

    def _current(self, pr: PullRequest) -> PullRequest:
        """``pr`` with the diff its branch has now: a push after it was opened changes it."""
        if pr.state != "open" or not branch_exists(self._directory, pr.repo, pr.branch):
            return pr
        return replace(pr, diff=branch_diff(self._directory, pr.repo, pr.branch))

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
