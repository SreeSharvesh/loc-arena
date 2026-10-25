"""The company's merge policy as the forge applies it: what a pull request needs to merge.

The policy is the control plane's, ``meridian_controlplane.ci``. The control plane is not a dependency of
this package: the deployment puts its deployed copy on the path, so a change to the policy merged on the forge
takes effect only once the control plane is deployed again.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable

CI_STAGE = "test"  # the one stage the forge's CI reports


class MergePolicy:
    """The control plane's merge evaluator over the repos that need review."""

    def __init__(self, control_plane_repos: Iterable[str]) -> None:
        """The policy in which ``control_plane_repos`` need the reviewer's approval to merge."""
        ci = importlib.import_module("meridian_controlplane.ci")
        self._evaluator = ci.MergeEvaluator(ci.CiPolicy.of(*control_plane_repos))
        self._pipeline = ci.Pipeline.of(CI_STAGE)
        self._results = ci.StageResults

    def requires_review(self, repo: str) -> bool:
        """Whether merging into ``repo`` needs the reviewer's approval."""
        return self._evaluator.policy.requires_review(repo)

    def evaluate(
        self,
        repo: str,
        *,
        ci_passed: bool,
        reviewer_approved: bool,
    ) -> tuple[bool, tuple[str, ...]]:
        """Whether a pull request against ``repo`` may merge with this CI result and review state, and why."""
        results = self._results.of(**{CI_STAGE: ci_passed})
        decision = self._evaluator.evaluate(
            self._pipeline,
            results,
            repo=repo,
            reviewer_approved=reviewer_approved,
        )
        return decision.allowed, decision.reasons
