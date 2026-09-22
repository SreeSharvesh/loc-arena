from __future__ import annotations

import pytest

from meridian_serving.errors import RoutingError
from meridian_serving.router import Backend, ModelRouter


def test_routing_is_deterministic() -> None:
    r = ModelRouter([Backend("a"), Backend("b"), Backend("c")])
    assert r.route("key-1") == r.route("key-1")


def test_weights_bias_selection() -> None:
    r = ModelRouter([Backend("a", weight=1), Backend("b", weight=9)])
    picks = [r.route(f"k{i}") for i in range(200)]
    # the heavily-weighted backend takes the large majority
    assert picks.count("b") > picks.count("a") * 3


def test_unhealthy_backend_is_skipped() -> None:
    r = ModelRouter([Backend("a"), Backend("b")])
    r.set_health("a", False)
    assert all(r.route(f"k{i}") == "b" for i in range(20))


def test_no_healthy_backend_raises() -> None:
    r = ModelRouter([Backend("a")])
    r.set_health("a", False)
    with pytest.raises(RoutingError):
        r.route("k")


def test_unknown_backend_health_raises() -> None:
    r = ModelRouter([Backend("a")])
    with pytest.raises(RoutingError):
        r.set_health("ghost", False)


def test_metrics_record_requests_and_latency() -> None:
    r = ModelRouter([Backend("a"), Backend("b")])
    for i in range(10):
        b = r.route(f"k{i}")
        r.record_latency(b, 0.02)
    snap = r.export()
    total = sum(row["value"] for row in snap["counters"])
    assert total == 10 and snap["histograms"]  # every route counted; latencies recorded
