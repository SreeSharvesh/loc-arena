"""Tests for the evalkit metrics: classification, calibration, embedding, registry, accumulator."""

from __future__ import annotations

import pytest

from meridian_evalkit.errors import MetricError
from meridian_evalkit.metrics import (
    RunningAccuracy,
    ScoreRecord,
    accuracy,
    brier_score,
    default_registry,
    embed,
    error_rate,
    expected_calibration_error,
    pass_at_k,
    reference_embedding_similarity,
)
from meridian_evalkit.metrics.registry import MetricRegistry


def _rec(item_id: str, correct: bool, confidence: float = 1.0, **kw: object) -> ScoreRecord:
    return ScoreRecord(item_id=item_id, correct=correct, confidence=confidence, **kw)  # type: ignore[arg-type]


def test_accuracy_half() -> None:
    records = [_rec("a", True), _rec("b", True), _rec("c", False), _rec("d", False)]
    assert accuracy(records) == 0.5
    assert error_rate(records) == 0.5


def test_accuracy_empty_raises() -> None:
    with pytest.raises(MetricError):
        accuracy([])


def test_pass_at_k_unbiased() -> None:
    rec = ScoreRecord(item_id="a", correct=True, samples=(True, False, True, False))
    assert pass_at_k([rec], 1) == pytest.approx(0.5)
    assert pass_at_k([rec], 2) == pytest.approx(5 / 6)


def test_pass_at_k_all_wrong_is_zero() -> None:
    rec = ScoreRecord(item_id="a", correct=False, samples=(False, False, False))
    assert pass_at_k([rec], 2) == 0.0


def test_pass_at_k_all_right_is_one() -> None:
    rec = ScoreRecord(item_id="a", correct=True, samples=(True, True))
    assert pass_at_k([rec], 1) == 1.0


def test_pass_at_k_bad_k_raises() -> None:
    with pytest.raises(MetricError):
        pass_at_k([_rec("a", True)], 0)


def test_pass_at_k_default_samples_from_correct() -> None:
    # a record with no explicit samples supplies its primary outcome as a single sample
    assert pass_at_k([_rec("a", True)], 1) == 1.0
    assert pass_at_k([_rec("a", False)], 1) == 0.0


def test_ece_single_bin() -> None:
    records = [_rec("a", True, 0.9), _rec("b", False, 0.9)]
    assert expected_calibration_error(records, n_bins=10) == pytest.approx(0.4)


def test_ece_perfect_calibration_is_zero() -> None:
    records = [_rec("a", True, 1.0), _rec("b", True, 1.0)]
    assert expected_calibration_error(records, n_bins=10) == pytest.approx(0.0)


def test_ece_empty_raises() -> None:
    with pytest.raises(MetricError):
        expected_calibration_error([])


def test_brier_score() -> None:
    records = [_rec("a", True, 0.9), _rec("b", False, 0.9)]
    assert brier_score(records) == pytest.approx(0.41)


def test_embed_normalized() -> None:
    vec = embed((1, 2, 3, 4))
    norm = sum(v * v for v in vec) ** 0.5
    assert norm == pytest.approx(1.0)


def test_embed_empty_is_zero() -> None:
    assert embed(()) == tuple(0.0 for _ in range(16))


def test_reference_embedding_identical_is_one() -> None:
    records = [ScoreRecord(item_id="a", correct=True, prediction=(1, 2, 3), reference=(1, 2, 3))]
    assert reference_embedding_similarity(records) == pytest.approx(1.0)


def test_reference_embedding_recomputed_deterministic() -> None:
    records = [ScoreRecord(item_id="a", correct=False, prediction=(5, 6), reference=(1, 2, 3))]
    first = reference_embedding_similarity(records)
    second = reference_embedding_similarity(records)
    assert first == second


def test_reference_embedding_empty_reference_contributes_zero() -> None:
    records = [ScoreRecord(item_id="a", correct=False, prediction=(5, 6), reference=())]
    assert reference_embedding_similarity(records) == 0.0


def test_registry_default_names() -> None:
    reg = default_registry(pass_k=(1, 2))
    assert reg.names() == (
        "accuracy",
        "brier",
        "ece",
        "error_rate",
        "pass@1",
        "pass@2",
        "reference_embedding",
    )


def test_registry_evaluate_all() -> None:
    reg = default_registry()
    records = [
        ScoreRecord(item_id="a", correct=True, confidence=0.9, prediction=(1,), reference=(1,)),
        ScoreRecord(item_id="b", correct=False, confidence=0.4, prediction=(2,), reference=(3,)),
    ]
    out = reg.evaluate_all(records)
    assert out["accuracy"] == 0.5
    assert set(out) == set(reg.names())


def test_registry_unknown_metric_raises() -> None:
    with pytest.raises(MetricError):
        MetricRegistry().get("nope")


def test_registry_duplicate_registration_raises() -> None:
    reg = MetricRegistry()
    reg.register("m", accuracy)
    with pytest.raises(MetricError):
        reg.register("m", accuracy)


def test_registry_metric_orientation() -> None:
    reg = default_registry()
    assert reg.get("accuracy").higher_is_better is True
    assert reg.get("ece").higher_is_better is False


def test_running_accuracy_single_pass() -> None:
    ra = RunningAccuracy()
    ra.extend([_rec("a", True), _rec("b", True), _rec("c", False), _rec("d", False)])
    assert ra.value == 0.5
    assert ra.distinct_items == 4
    assert ra.observations == 4


def test_running_accuracy_empty_raises() -> None:
    with pytest.raises(MetricError):
        _ = RunningAccuracy().value
