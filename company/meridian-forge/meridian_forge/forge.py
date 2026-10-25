"""The forge's pull requests and merges over what it hosts, and its ``git``, ticket and wiki reads.

It operates ``git``/``open_pr``/``pr_status``/``merge``/``read_ticket``/``list_tickets``/``update_ticket``/
``search_wiki``/``read_wiki``. The merge gate: once a pull request is open and its CI has finished, the
company's merge policy decides, given CI's result and, on a repo it says needs review, the reviewer's verdict.
Its caller records what is done on it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from meridian_forge.ci import CiStatus, CiTiming, ci_status
from meridian_forge.merge_policy import MergePolicy
from meridian_forge.models import PullRequest, SeededWorld, Ticket
from meridian_forge.review import PullRequestReviewer


class ForgeError(ValueError):
    """A forge operation referenced something that does not exist (unknown repo, PR, ticket, or page)."""


class Forge:
    """The forge over one ``SeededWorld``: what it hosts, and the pull requests and changes made since."""

    def __init__(
        self,
        world: SeededWorld,
        *,
        reviewer: PullRequestReviewer,
        policy: MergePolicy,
        ci: CiTiming | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Host ``world`` and merge under ``policy``, with ``reviewer``'s verdict where it needs review.

        A pull request opened without a declared CI status waits on CI for ``ci``'s wait (none by default) by
        ``clock``'s time.
        """
        self._world = world
        self._reviewer = reviewer
        self._policy = policy
        self._ci = ci or CiTiming()
        self._clock = clock
        # what changed on the forge since, over the world it was given
        self._prs: dict[tuple[str, int], PullRequest] = {
            (pr.repo, pr.number): pr for r in world.repos.values() for pr in r.pull_requests
        }
        self._tickets: dict[str, Ticket] = {t.id: t for t in world.tickets}
        self._next_number: dict[str, int] = {
            name: (max((pr.number for pr in r.pull_requests), default=0) + 1)
            for name, r in world.repos.items()
        }

    # -- read-only repo/world surface -----------------------------------------------------------------
    @property
    def world(self) -> SeededWorld:
        """What this forge was given to host (its base state, unchanged)."""
        return self._world

    def _require_repo(self, repo: str) -> None:
        if repo not in self._world.repos:
            known = ", ".join(sorted(self._world.repos)) or "(none)"
            raise ForgeError(f"unknown repo {repo!r}; hosted: {known}")

    def git(self, args: dict[str, Any]) -> dict[str, Any]:
        """A read-only ``git`` subset over a repo's history: ``log``, ``branch``, ``show``."""
        repo = str(args.get("repo", ""))
        self._require_repo(repo)
        sub = str(args.get("subcommand", args.get("cmd", "log")))
        r = self._world.repos[repo]
        if sub == "log":
            n = int(args.get("n", 10))
            return {
                "repo": repo,
                "commits": [
                    {"sha": c.sha, "author": c.author, "date": c.date, "message": c.message}
                    for c in reversed(r.commits[-n:])
                ],
                "head": r.head,
            }
        if sub == "branch":
            return {"repo": repo, "branches": [b.name for b in r.branches], "head": r.head}
        if sub == "show":
            return {"repo": repo, "changelog": r.changelog, "codeowners": list(r.codeowners)}
        return {"repo": repo, "error": f"unsupported git subcommand {sub!r}"}

    # -- pull requests ---------------------------------------------------------------------------------
    def list_prs(self, repo: str | None = None) -> list[dict[str, Any]]:
        """Every open PR (optionally one repo), as observable summaries in a stable order."""
        prs = sorted(
            (pr for pr in self._prs.values() if pr.state == "open" and (repo is None or pr.repo == repo)),
            key=lambda p: (p.repo, p.number),
        )
        return [self._pr_summary(pr) for pr in prs]

    def _pr_summary(self, pr: PullRequest) -> dict[str, Any]:
        return {
            "number": pr.number,
            "repo": pr.repo,
            "title": pr.title,
            "author": pr.author,
            "state": pr.state,
            "ci": ci_status(pr, self._clock()),
            "control_plane": pr.control_plane,
            "labels": list(pr.labels),
        }

    def open_pr(self, args: dict[str, Any]) -> dict[str, Any]:
        """Open a new PR against a hosted repo and return its number and observable fields.

        A PR declaring no CI status (one opened through the forge's tools) is pending until its wait on CI
        has passed.
        """
        repo = str(args.get("repo", ""))
        self._require_repo(repo)
        control_plane = self._world.repos[repo].control_plane
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
            ci_passes_at=self._clock() + self._ci.wait(repo, number) if declared is None else None,
            control_plane=control_plane,
            reduces_coverage=bool(args.get("reduces_coverage", False)),
        )
        self._prs[(repo, number)] = pr
        return {"opened": True, **self._pr_summary(pr)}

    def pr_status(self, args: dict[str, Any]) -> dict[str, Any]:
        """The status of a PR: its CI, its review where the policy needs one, and whether it may merge now."""
        pr = self._get_pr(args)
        ci = ci_status(pr, self._clock())
        approved, review_reason = self._review(pr)
        mergeable, _ = self._merge_gate(pr, ci, approved, review_reason)
        return {
            **self._pr_summary(pr),
            "approved": approved,
            "review_reason": review_reason,
            "mergeable": mergeable,
        }

    def merge(self, args: dict[str, Any]) -> dict[str, Any]:
        """Merge if the gate allows it, and say why it did or did not."""
        pr = self._get_pr(args)
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
        decision = self._policy.evaluate(pr.repo, ci_passed=ci == "green", reviewer_approved=approved)
        return decision.allowed, "; ".join(
            decision.reasons if approved else (review_reason, *decision.reasons),
        )

    def _get_pr(self, args: dict[str, Any]) -> PullRequest:
        repo = str(args.get("repo", ""))
        self._require_repo(repo)
        number = int(args.get("number", args.get("pr", 0)))
        key = (repo, number)
        if key not in self._prs:
            raise ForgeError(f"unknown PR #{number} in {repo!r}")
        return self._prs[key]

    # -- tickets ---------------------------------------------------------------------------------------
    def list_tickets(self, args: dict[str, Any]) -> dict[str, Any]:
        """List backlog tickets, optionally filtered by state or label (stable id order)."""
        state = args.get("state")
        label = args.get("label")
        rows = [
            t
            for t in sorted(self._tickets.values(), key=lambda t: t.id)
            if (state is None or t.state == state) and (label is None or label in t.labels)
        ]
        return {"tickets": [self._ticket_summary(t) for t in rows], "count": len(rows)}

    def read_ticket(self, args: dict[str, Any]) -> dict[str, Any]:
        """Read one ticket in full by id."""
        tid = str(args.get("id", args.get("ticket", "")))
        if tid not in self._tickets:
            raise ForgeError(f"unknown ticket {tid!r}")
        t = self._tickets[tid]
        return {**self._ticket_summary(t), "body": t.body, "refs": list(t.refs)}

    def update_ticket(self, args: dict[str, Any]) -> dict[str, Any]:
        """Update a ticket's state or assignee (an ordinary tamperable write)."""
        tid = str(args.get("id", args.get("ticket", "")))
        if tid not in self._tickets:
            raise ForgeError(f"unknown ticket {tid!r}")
        t = self._tickets[tid]
        new = replace(
            t,
            state=str(args.get("state", t.state)),
            assignee=args.get("assignee", t.assignee),
        )
        self._tickets[tid] = new
        return {"updated": True, **self._ticket_summary(new)}

    @staticmethod
    def _ticket_summary(t: Ticket) -> dict[str, Any]:
        return {
            "id": t.id,
            "title": t.title,
            "state": t.state,
            "priority": t.priority,
            "labels": list(t.labels),
            "assignee": t.assignee,
        }

    # -- wiki ------------------------------------------------------------------------------------------
    def read_wiki(self, args: dict[str, Any]) -> dict[str, Any]:
        """Read one wiki page by slug."""
        slug = str(args.get("slug", args.get("page", "")))
        for page in self._world.wiki:
            if page.slug == slug:
                return {"slug": page.slug, "title": page.title, "body": page.body, "stale": page.stale}
        raise ForgeError(f"unknown wiki page {slug!r}")

    def search_wiki(self, args: dict[str, Any]) -> dict[str, Any]:
        """Search wiki page titles/bodies for a query term (case-insensitive), returning matching slugs."""
        q = str(args.get("query", args.get("q", ""))).lower()
        hits = [
            {"slug": p.slug, "title": p.title, "stale": p.stale}
            for p in self._world.wiki
            if q in p.title.lower() or q in p.body.lower()
        ]
        return {"query": q, "hits": hits}

    # -- spend ledger ----------------------------------------------------------------------------------
    def ledger(self) -> list[dict[str, Any]]:
        """The per-identity spend/quota ledger (partly consumed), as observable rows."""
        return [
            {
                "identity": e.identity,
                "compute_spent": e.compute_spent,
                "compute_quota": e.compute_quota,
                "inference_spent": e.inference_spent,
                "inference_quota": e.inference_quota,
            }
            for e in self._world.ledger
        ]
