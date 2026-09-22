"""Tests for the versioned results store and run diffing."""

from __future__ import annotations

import pytest

from meridian_evalkit.errors import StoreError, VersionNotFoundError
from meridian_evalkit.harness import DeterministicModel, EvalItem, Harness
from meridian_evalkit.store import EvalRun, ResultsStore, diff_runs


def _run(run_id: str, model: str, **metrics: float) -> EvalRun:
    return EvalRun(run_id=run_id, model=model, metrics=dict(metrics), item_ids=("a", "b"))


def test_append_returns_monotonic_versions() -> None:
    store = ResultsStore()
    assert store.append(_run("r1", "m", accuracy=0.5)) == 1
    assert store.append(_run("r2", "m", accuracy=0.6)) == 2
    assert store.versions() == (1, 2)
    assert len(store) == 2


def test_get_by_version() -> None:
    store = ResultsStore()
    store.append(_run("r1", "m", accuracy=0.5))
    assert store.get(1).run_id == "r1"


def test_latest() -> None:
    store = ResultsStore()
    store.append(_run("r1", "m", accuracy=0.5))
    store.append(_run("r2", "m", accuracy=0.6))
    assert store.latest().run_id == "r2"
    assert store.latest_version == 2


def test_empty_store_latest_raises() -> None:
    with pytest.raises(StoreError):
        _ = ResultsStore().latest_version


def test_get_out_of_range_raises() -> None:
    store = ResultsStore()
    store.append(_run("r1", "m", accuracy=0.5))
    with pytest.raises(VersionNotFoundError):
        store.get(0)
    with pytest.raises(VersionNotFoundError):
        store.get(99)


def test_versions_for_run_id() -> None:
    store = ResultsStore()
    store.append(_run("r1", "m", accuracy=0.5))
    store.append(_run("r2", "m", accuracy=0.6))
    store.append(_run("r1", "m", accuracy=0.7))
    assert store.versions_for("r1") == (1, 3)


def test_fingerprint_stable_for_equal_runs() -> None:
    assert _run("r1", "m", accuracy=0.5).fingerprint == _run("r1", "m", accuracy=0.5).fingerprint


def test_fingerprint_differs_on_metric_change() -> None:
    assert _run("r1", "m", accuracy=0.5).fingerprint != _run("r1", "m", accuracy=0.6).fingerprint


def test_diff_metric_deltas() -> None:
    left = EvalRun(run_id="r1", model="m", metrics={"accuracy": 0.5, "ece": 0.2}, item_ids=("a", "b"))
    right = EvalRun(run_id="r2", model="m", metrics={"accuracy": 0.7, "ece": 0.2}, item_ids=("a", "c"))
    d = diff_runs(left, right)
    assert d.delta("accuracy") == pytest.approx(0.2)
    assert d.delta("ece") == pytest.approx(0.0)
    assert d.added_items == ("c",)
    assert d.removed_items == ("b",)
    assert not d.unchanged


def test_diff_added_removed_metrics() -> None:
    left = EvalRun(run_id="r1", model="m", metrics={"accuracy": 0.5}, item_ids=())
    right = EvalRun(run_id="r2", model="m", metrics={"brier": 0.1}, item_ids=())
    d = diff_runs(left, right)
    assert d.added_metrics == ("brier",)
    assert d.removed_metrics == ("accuracy",)
    assert d.metric_deltas == ()


def test_diff_unchanged() -> None:
    run = EvalRun(run_id="r1", model="m", metrics={"accuracy": 0.5}, item_ids=("a",))
    assert diff_runs(run, run).unchanged


def test_store_diff_uses_versions() -> None:
    store = ResultsStore()
    store.append(_run("r1", "m", accuracy=0.5))
    store.append(_run("r2", "m", accuracy=0.9))
    assert store.diff(1, 2).delta("accuracy") == pytest.approx(0.4)


def test_eval_run_from_report() -> None:
    items = [EvalItem(item_id=f"q{i}", prompt=(1, i), reference=(i,)) for i in range(4)]
    report = Harness().run(DeterministicModel(accuracy=1.0), items)
    run = EvalRun.from_report("run-1", "det", report)
    assert run.item_ids == ("q0", "q1", "q2", "q3")
    assert run.metrics["accuracy"] == 1.0


def test_eval_run_json_round_trip() -> None:
    run = EvalRun(run_id="r1", model="det", metrics={"accuracy": 0.5, "ece": 0.2}, item_ids=("a", "b"))
    restored = EvalRun.from_payload(run.to_payload())
    assert restored == run
    assert restored.fingerprint == run.fingerprint


def test_eval_run_to_json_is_canonical() -> None:
    left = EvalRun(run_id="r1", model="det", metrics={"accuracy": 0.5, "ece": 0.2}, item_ids=("a",))
    right = EvalRun(run_id="r1", model="det", metrics={"ece": 0.2, "accuracy": 0.5}, item_ids=("a",))
    assert left.to_json() == right.to_json()


def test_eval_run_from_bad_payload_raises() -> None:
    with pytest.raises(StoreError):
        EvalRun.from_payload({"run_id": "r1"})
