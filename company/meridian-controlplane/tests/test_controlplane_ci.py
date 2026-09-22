from __future__ import annotations

import pytest

from meridian_controlplane.ci import (
    CiPolicy,
    MergeEvaluator,
    Pipeline,
    Stage,
    StageResults,
)
from meridian_controlplane.errors import CiConfigError


def _pipeline() -> Pipeline:
    return Pipeline.of("lint", "typecheck", "test")


def test_pipeline_rejects_empty_and_duplicate_stages() -> None:
    with pytest.raises(CiConfigError):
        Pipeline(stages=())
    with pytest.raises(CiConfigError):
        Pipeline(stages=(Stage("test"), Stage("test")))


def test_stage_results_green_gate() -> None:
    pipeline = _pipeline()
    green = StageResults.of(lint=True, typecheck=True, test=True)
    red = StageResults.of(lint=True, typecheck=False, test=True)
    assert green.all_required_green(pipeline)
    assert not red.all_required_green(pipeline)
    assert red.failed_required(pipeline) == ("typecheck",)


def test_advisory_stage_does_not_block_gate() -> None:
    pipeline = Pipeline(stages=(Stage("test"), Stage("benchmark", required=False)))
    results = StageResults.of(test=True, benchmark=False)
    assert results.all_required_green(pipeline)


def test_ordinary_repo_merges_on_green_ci_without_review() -> None:
    evaluator = MergeEvaluator(CiPolicy.of("meridian-controlplane"))
    decision = evaluator.evaluate(
        _pipeline(),
        StageResults.of(lint=True, typecheck=True, test=True),
        repo="meridian-serving",
        reviewer_approved=False,
    )
    assert decision.allowed


def test_control_plane_repo_requires_reviewer_approval() -> None:
    evaluator = MergeEvaluator(CiPolicy.of("meridian-controlplane"))
    green = StageResults.of(lint=True, typecheck=True, test=True)
    blocked = evaluator.evaluate(_pipeline(), green, repo="meridian-controlplane", reviewer_approved=False)
    assert not blocked.allowed
    assert any("reviewer approval" in reason for reason in blocked.reasons)
    approved = evaluator.evaluate(_pipeline(), green, repo="meridian-controlplane", reviewer_approved=True)
    assert approved.allowed


def test_control_plane_repo_still_needs_green_ci() -> None:
    evaluator = MergeEvaluator(CiPolicy.of("meridian-controlplane"))
    red = StageResults.of(lint=True, typecheck=True, test=False)
    decision = evaluator.evaluate(_pipeline(), red, repo="meridian-controlplane", reviewer_approved=True)
    assert not decision.allowed
    assert any("CI stages failed" in reason for reason in decision.reasons)


def test_may_merge_is_the_boolean_of_evaluate() -> None:
    evaluator = MergeEvaluator(CiPolicy.of("meridian-controlplane"))
    green = StageResults.of(lint=True, typecheck=True, test=True)
    assert not evaluator.may_merge(_pipeline(), green, repo="meridian-controlplane", reviewer_approved=False)
    assert evaluator.may_merge(_pipeline(), green, repo="meridian-serving", reviewer_approved=False)
