"""The `timing` axis and the aggregation fields parse, default, and validate on MonitorSpec."""

from __future__ import annotations

import pytest
from loc_arena.monitors.registry import _spec_from, load_monitor_specs


def _entry(**over: object) -> dict[str, object]:
    base: dict[str, object] = {"name": "m", "tap": ["action"], "inputs": ["actor_id"], "enabled": True}
    base.update(over)
    return base


def test_timing_defaults_to_sync() -> None:
    spec = _spec_from(_entry())
    assert spec.timing == "sync"
    assert spec.per_monitor_reduce is None
    assert spec.weight == 1.0
    assert spec.timeout_s is None


def test_timing_async_parses_with_agg_fields() -> None:
    spec = _spec_from(_entry(timing="async", per_monitor_reduce="mean", weight=0.0, timeout_s=7))
    assert spec.timing == "async"
    assert spec.per_monitor_reduce == "mean"
    assert spec.weight == 0.0
    assert spec.timeout_s == 7.0


def test_invalid_timing_raises_clear_error() -> None:
    with pytest.raises(ValueError, match="timing must be 'sync' or 'async'"):
        _spec_from(_entry(timing="online"))


def test_shipped_monitors_yaml_specs_have_valid_timing() -> None:
    for spec in load_monitor_specs("configs/monitors.yaml"):
        assert spec.timing in ("sync", "async")
