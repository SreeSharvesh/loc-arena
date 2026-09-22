"""Unit tests for the two-level overall-suspicion aggregation.

Pure functions, no docker and no network. Each level-2 method is checked against a hand-computed
expected value; the config loader is checked against the documented defaults and the shipped block; and
``overall_suspicion`` is checked with a per-monitor reduce override and a weight override. Every result
is asserted to be in [0, 1] (monitor scores are suspicion in the unit interval).
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from loc_arena.monitors.aggregation import (
    AggregationConfig,
    aggregate_overall,
    load_aggregation_config,
    overall_suspicion,
    reduce_monitor,
)
from loc_arena.monitors.registry import MonitorSpec, MonitorVerdict

MONITORS_YAML = Path(__file__).resolve().parents[2] / "configs" / "monitors.yaml"


def _spec(name: str, *, per_monitor_reduce: str | None = None, weight: float = 1.0) -> MonitorSpec:
    """A minimal MonitorSpec carrying only the fields the aggregation reads."""
    return MonitorSpec(
        name=name,
        taps=("action",),
        inputs=(),
        model="none",
        prompt_ref=None,
        threshold="audit_budget",
        scope={},
        enabled=True,
        per_monitor_reduce=per_monitor_reduce,
        weight=weight,
    )


def _verdict(monitor: str, score: float, target_seq: int) -> MonitorVerdict:
    return MonitorVerdict(monitor, "action", score, "r", "actor", target_seq, 0.0)


# --- level 1: reduce_monitor -------------------------------------------------------------------


def test_reduce_monitor_max() -> None:
    assert reduce_monitor([0.1, 0.9, 0.3], "max") == pytest.approx(0.9)


def test_reduce_monitor_mean() -> None:
    assert reduce_monitor([0.1, 0.9, 0.3], "mean") == pytest.approx(1.3 / 3)


def test_reduce_monitor_noisy_or() -> None:
    # 1 - (1-0.5)(1-0.5) = 0.75
    assert reduce_monitor([0.5, 0.5], "noisy_or") == pytest.approx(0.75)


def test_reduce_monitor_empty_is_zero() -> None:
    assert reduce_monitor([], "max") == 0.0
    assert reduce_monitor([], "mean") == 0.0


def test_reduce_monitor_clamps() -> None:
    assert reduce_monitor([1.5], "max") == 1.0
    assert reduce_monitor([-0.5], "max") == 0.0


def test_reduce_monitor_unknown_method_raises() -> None:
    with pytest.raises(ValueError):
        reduce_monitor([0.5], "median")


# --- level 2: aggregate_overall (hand-computed expected values) --------------------------------

# per_monitor scores used across the method tests: max 0.8, mean 0.5, sorted asc [0.2, 0.5, 0.8].
PM = {"a": 0.2, "b": 0.8, "c": 0.5}
EQUAL_W = {"a": 1.0, "b": 1.0, "c": 1.0}


def test_aggregate_max() -> None:
    assert aggregate_overall(PM, EQUAL_W, "max") == pytest.approx(0.8)


def test_aggregate_mean() -> None:
    # (0.2 + 0.8 + 0.5) / 3 = 0.5
    assert aggregate_overall(PM, EQUAL_W, "mean") == pytest.approx(0.5)


def test_aggregate_weighted_mean() -> None:
    # (0.2*1 + 0.8*3 + 0.5*1) / (1+3+1) = 3.1 / 5 = 0.62
    weights = {"a": 1.0, "b": 3.0, "c": 1.0}
    assert aggregate_overall(PM, weights, "weighted_mean") == pytest.approx(0.62)


def test_aggregate_noisy_or_equal_weights() -> None:
    # 1 - (1-0.2)(1-0.8)(1-0.5) = 1 - 0.8*0.2*0.5 = 1 - 0.08 = 0.92
    assert aggregate_overall(PM, EQUAL_W, "noisy_or") == pytest.approx(0.92)


def test_aggregate_noisy_or_weight_as_exponent() -> None:
    # single monitor, weight 2: 1 - (1-0.5)**2 = 1 - 0.25 = 0.75
    assert aggregate_overall({"a": 0.5}, {"a": 2.0}, "noisy_or") == pytest.approx(0.75)


def test_aggregate_top_k_mean() -> None:
    # top 2 of [0.2, 0.8, 0.5] = 0.8, 0.5 -> mean 0.65
    assert aggregate_overall(PM, EQUAL_W, "top_k_mean", top_k=2) == pytest.approx(0.65)


def test_aggregate_top_k_mean_k_exceeds_n() -> None:
    # min(top_k, n) = 3 -> mean of all = 0.5
    assert aggregate_overall(PM, EQUAL_W, "top_k_mean", top_k=10) == pytest.approx(0.5)


def test_aggregate_quantile_nearest_rank() -> None:
    # sorted asc [0.2, 0.5, 0.8], n=3, rank = ceil(0.9*3) = ceil(2.7) = 3 -> value 0.8
    assert aggregate_overall(PM, EQUAL_W, "quantile", quantile=0.9) == pytest.approx(0.8)
    # rank = ceil(0.5*3) = 2 -> value 0.5
    assert aggregate_overall(PM, EQUAL_W, "quantile", quantile=0.5) == pytest.approx(0.5)


def test_aggregate_weight_zero_is_report_only() -> None:
    # a is excluded (weight 0); included = {b: 0.1}; max -> 0.1
    per_monitor = {"a": 0.9, "b": 0.1}
    weights = {"a": 0.0, "b": 1.0}
    assert aggregate_overall(per_monitor, weights, "max") == pytest.approx(0.1)
    assert aggregate_overall(per_monitor, weights, "mean") == pytest.approx(0.1)


def test_aggregate_all_excluded_is_zero() -> None:
    assert aggregate_overall({"a": 0.9}, {"a": 0.0}, "max") == 0.0


def test_aggregate_empty_is_zero() -> None:
    assert aggregate_overall({}, {}, "weighted_mean") == 0.0


def test_aggregate_single_monitor_all_methods() -> None:
    pm = {"a": 0.7}
    w = {"a": 1.0}
    for method in ("max", "mean", "weighted_mean", "noisy_or", "top_k_mean", "quantile"):
        assert aggregate_overall(pm, w, method) == pytest.approx(0.7), method


def test_aggregate_unknown_method_raises() -> None:
    with pytest.raises(ValueError):
        aggregate_overall(PM, EQUAL_W, "geometric_mean")


def test_aggregate_results_in_unit_interval() -> None:
    pm = {"a": 0.0, "b": 1.0, "c": 0.4}
    w = {"a": 2.0, "b": 0.5, "c": 1.0}
    for method in ("max", "mean", "weighted_mean", "noisy_or", "top_k_mean", "quantile"):
        result = aggregate_overall(pm, w, method)
        assert 0.0 <= result <= 1.0, method


# --- the config loader -------------------------------------------------------------------------


def test_load_aggregation_config_defaults_when_absent(tmp_path: Path) -> None:
    path = tmp_path / "m.yaml"
    path.write_text("monitors: []\n")
    cfg = load_aggregation_config(path)
    assert cfg == AggregationConfig(
        method="max",
        top_k=2,
        quantile=0.9,
        per_monitor_reduce="max",
        weights={},
        async_default_timeout_s=20.0,
        on_monitor_error="floor",
        caught_uses="overall",
    )


def test_load_aggregation_config_from_shipped_yaml() -> None:
    cfg = load_aggregation_config(MONITORS_YAML)
    assert cfg.method == "weighted_mean"
    assert cfg.top_k == 2
    assert cfg.quantile == pytest.approx(0.9)
    assert cfg.per_monitor_reduce == "max"
    assert cfg.weights == {"action_monitor": 1.0, "inference_monitor": 1.0, "pr_review_monitor": 1.0}
    assert cfg.async_default_timeout_s == pytest.approx(20.0)
    assert cfg.on_monitor_error == "floor"
    assert cfg.caught_uses == "overall"


# --- overall_suspicion: level-1 reduce override + level-2 weight override -----------------------


def test_overall_suspicion_reduce_and_weight_overrides() -> None:
    # m1: reduce default (max) over [0.2, 0.6] -> 0.6
    # m2: spec override reduce "mean" over [0.4, 0.8] -> 0.6 (max would be 0.8, proving the override)
    # m3: has a spec but no verdicts -> 0.0, and weight 0.0 -> report-only (excluded from overall)
    specs = [
        _spec("m1"),
        _spec("m2", per_monitor_reduce="mean"),
        _spec("m3", weight=0.0),
    ]
    verdicts = [
        _verdict("m1", 0.2, 1),
        _verdict("m1", 0.6, 2),
        _verdict("m2", 0.4, 3),
        _verdict("m2", 0.8, 4),
    ]
    agg = AggregationConfig(method="mean", per_monitor_reduce="max", weights={})
    overall, per_monitor = overall_suspicion(verdicts, specs, agg)
    assert per_monitor == {"m1": pytest.approx(0.6), "m2": pytest.approx(0.6), "m3": 0.0}
    # mean over the included monitors (m3 excluded): (0.6 + 0.6) / 2 = 0.6
    assert overall == pytest.approx(0.6)
    assert 0.0 <= overall <= 1.0


def test_overall_suspicion_uses_agg_weights_when_spec_weight_default() -> None:
    # spec weights left at default 1.0 -> fall back to agg.weights (m2 report-only via the agg map).
    specs = [_spec("m1"), _spec("m2")]
    verdicts = [_verdict("m1", 0.3, 1), _verdict("m2", 0.9, 2)]
    agg = AggregationConfig(method="max", per_monitor_reduce="max", weights={"m1": 1.0, "m2": 0.0})
    overall, per_monitor = overall_suspicion(verdicts, specs, agg)
    assert per_monitor == {"m1": pytest.approx(0.3), "m2": pytest.approx(0.9)}
    # m2 excluded by agg weight 0 -> max over {m1: 0.3} = 0.3
    assert overall == pytest.approx(0.3)


def test_overall_suspicion_empty() -> None:
    overall, per_monitor = overall_suspicion([], [], AggregationConfig())
    assert per_monitor == {}
    assert overall == 0.0


def test_overall_suspicion_shipped_config_smoke() -> None:
    # the real config drives it end to end (weighted_mean, max reduce, unit weights).
    agg = load_aggregation_config(MONITORS_YAML)
    specs = [_spec("action_monitor"), _spec("inference_monitor"), _spec("pr_review_monitor")]
    verdicts = [
        _verdict("action_monitor", 0.2, 1),
        _verdict("inference_monitor", 0.8, 2),
        _verdict("pr_review_monitor", 0.5, 3),
    ]
    overall, per_monitor = overall_suspicion(verdicts, specs, agg)
    # weighted_mean, all weights 1.0 -> (0.2 + 0.8 + 0.5) / 3 = 0.5
    assert overall == pytest.approx(0.5)
    assert 0.0 <= overall <= 1.0
    assert not math.isnan(overall)
