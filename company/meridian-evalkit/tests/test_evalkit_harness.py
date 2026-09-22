"""Tests for the benchmark harness and its deterministic reference model."""

from __future__ import annotations

import pytest

from meridian_distill.teacher import DEFAULT_FEATURE_DIM
from meridian_evalkit.errors import HarnessError
from meridian_evalkit.harness import (
    DeterministicModel,
    EvalItem,
    Harness,
    Prediction,
)


def _items(n: int) -> list[EvalItem]:
    return [EvalItem(item_id=f"q{i}", prompt=(1, 2, i % 5), reference=(i % 7, (i + 1) % 7)) for i in range(n)]


def test_deterministic_model_perfect_accuracy() -> None:
    model = DeterministicModel(accuracy=1.0)
    report = Harness().run(model, _items(20))
    assert report.metrics["accuracy"] == 1.0
    assert report.metrics["reference_embedding"] == pytest.approx(1.0)


def test_deterministic_model_zero_accuracy() -> None:
    model = DeterministicModel(accuracy=0.0)
    report = Harness().run(model, _items(20))
    assert report.metrics["accuracy"] == 0.0


def test_deterministic_model_matches_target_rate() -> None:
    model = DeterministicModel(accuracy=0.6, seed=1)
    items = [EvalItem(item_id=f"z{i}", prompt=(i % 9,), reference=((i * 3) % 11,)) for i in range(200)]
    report = Harness().run(model, items)
    assert report.metrics["accuracy"] == pytest.approx(0.6, abs=0.05)


def test_model_is_deterministic_across_runs() -> None:
    model = DeterministicModel(accuracy=0.5, seed=2)
    items = _items(30)
    first = Harness().run(model, items)
    second = Harness().run(model, items)
    assert first.metrics == second.metrics
    assert first.features() == second.features()


def test_report_features_have_default_dim() -> None:
    report = Harness().run(DeterministicModel(accuracy=1.0), _items(5))
    for vec in report.features().values():
        assert len(vec) == DEFAULT_FEATURE_DIM


def test_report_records_round_trip() -> None:
    report = Harness().run(DeterministicModel(accuracy=1.0), _items(5))
    records = report.records()
    assert len(records) == report.size == 5
    assert all(r.correct for r in records)


def test_score_item_mismatched_prediction_raises() -> None:
    item = EvalItem(item_id="a", prompt=(1,), reference=(2,))
    bad = Prediction(item_id="b", tokens=(2,))
    with pytest.raises(HarnessError):
        Harness().score_item(item, bad)


def test_run_empty_items_raises() -> None:
    with pytest.raises(HarnessError):
        Harness().run(DeterministicModel(), [])


def test_prediction_rejects_out_of_range_confidence() -> None:
    with pytest.raises(HarnessError):
        Prediction(item_id="a", tokens=(1,), confidence=1.5)


def test_model_accuracy_out_of_range_raises() -> None:
    with pytest.raises(HarnessError):
        DeterministicModel(accuracy=2.0)


def test_correct_prediction_echoes_reference() -> None:
    item = EvalItem(item_id="a", prompt=(1, 2), reference=(3, 4))
    result = Harness().score_item(item, DeterministicModel(accuracy=1.0).predict(item))
    assert result.correct
    assert result.prediction == (3, 4)
