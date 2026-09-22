from __future__ import annotations

import pytest

from meridian_common.errors import ValidationError
from meridian_common.metrics import Counter, Histogram, MetricsRegistry


def test_counter_only_increases() -> None:
    c = Counter("requests", {"route": "/serve"})
    c.inc()
    c.inc(4)
    assert c.value == 5.0
    with pytest.raises(ValueError):
        c.inc(-1)


def test_histogram_buckets_sum_count_and_quantile() -> None:
    h = Histogram("latency", [0.1, 0.5, 1.0])
    for v in [0.05, 0.2, 0.2, 0.9, 5.0]:
        h.observe(v)
    snap = h.snapshot()
    assert snap.count == 5 and snap.sum == pytest.approx(6.35)
    assert snap.buckets[-1] == (float("inf"), 5)  # everything counted
    assert h.quantile(0.5) == pytest.approx(0.2)  # median sample
    assert h.quantile(1.0) == pytest.approx(5.0)


def test_registry_is_idempotent_per_identity() -> None:
    reg = MetricsRegistry()
    a = reg.counter("hits", {"k": "v"})
    b = reg.counter("hits", {"k": "v"})
    assert a is b  # same identity -> same instrument
    reg.counter("hits", {"k": "other"}).inc(2)
    a.inc()
    collected = reg.collect()["counters"]
    assert {(r["name"], tuple(sorted(r["labels"].items())), r["value"]) for r in collected} == {
        ("hits", (("k", "v"),), 1.0),
        ("hits", (("k", "other"),), 2.0),
    }


def test_registry_rejects_type_conflict() -> None:
    reg = MetricsRegistry()
    reg.counter("x")
    with pytest.raises(ValidationError):
        reg.gauge("x")


def test_collect_is_sorted_and_typed() -> None:
    reg = MetricsRegistry()
    reg.gauge("g_b").set(2)
    reg.gauge("g_a").set(1)
    reg.histogram("h", [1.0]).observe(0.5)
    out = reg.collect()
    assert [r["name"] for r in out["gauges"]] == ["g_a", "g_b"]
    assert out["histograms"][0]["count"] == 1
