"""The merge evaluator: decide whether a pull request may merge.

A merge requires the pipeline's required stages to be green. In addition, a control-plane repo requires
reviewer approval, while other repos need green CI only. :class:`CiPolicy` holds which repos are control-plane
repos, and :class:`MergeEvaluator` combines the pipeline result, the repo's review requirement, and whether a
reviewer approved into a :class:`MergeDecision` with human-readable reasons.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from meridian_controlplane.ci.pipeline import Pipeline, StageResults, merge_reasons


@dataclass(frozen=True)
class MergeDecision:
    """Whether a pull request may merge, with the reasons behind the decision."""

    allowed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class CiPolicy:
    """Which repos are control-plane repos (reviewer approval required to merge).

    A repo named here requires green CI *and* reviewer approval; any other repo requires green CI only.
    """

    control_plane_repos: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def of(cls, *repos: str) -> CiPolicy:
        """A policy in which ``repos`` are the control-plane repos."""
        return cls(control_plane_repos=frozenset(repos))

    def requires_review(self, repo: str) -> bool:
        """Whether merging into ``repo`` requires reviewer approval."""
        return repo in self.control_plane_repos


class MergeEvaluator:
    """Decides whether a pull request may merge under a CI policy."""

    def __init__(self, policy: CiPolicy | None = None) -> None:
        """Use ``policy`` (or an empty policy where no repo requires review) for every decision."""
        self._policy = policy if policy is not None else CiPolicy()

    @property
    def policy(self) -> CiPolicy:
        """The CI policy this evaluator applies."""
        return self._policy

    def evaluate(
        self,
        pipeline: Pipeline,
        results: StageResults,
        *,
        repo: str,
        reviewer_approved: bool,
    ) -> MergeDecision:
        """Decide whether a PR against ``repo`` may merge given its CI results and review state."""
        ci_reasons: list[str] = []
        if not results.all_required_green(pipeline):
            failed = ", ".join(results.failed_required(pipeline))
            ci_reasons.append(f"required CI stages failed: {failed}")

        review_reasons: list[str] = []
        if self._policy.requires_review(repo) and not reviewer_approved:
            review_reasons.append("control-plane repo requires reviewer approval")

        allowed = not ci_reasons and not review_reasons
        if allowed:
            return MergeDecision(allowed=True, reasons=("all required stages green; review satisfied",))
        return MergeDecision(allowed=False, reasons=merge_reasons(ci_reasons, review_reasons))

    def may_merge(
        self,
        pipeline: Pipeline,
        results: StageResults,
        *,
        repo: str,
        reviewer_approved: bool,
    ) -> bool:
        """Whether a PR against ``repo`` may merge (the boolean of :meth:`evaluate`)."""
        return self.evaluate(pipeline, results, repo=repo, reviewer_approved=reviewer_approved).allowed
