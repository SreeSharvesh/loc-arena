from __future__ import annotations

from meridian_common.errors import TransportError
from meridian_serving.router import (
    Backend,
    HealthChecker,
    LeastLoadedBalancer,
    ModelRouter,
    PowerOfTwoBalancer,
    RetryingRouter,
    RoundRobinBalancer,
)


def test_round_robin_spreads_evenly() -> None:
    rr = RoundRobinBalancer()
    load = {"a": 0, "b": 0, "c": 0}
    picks = [rr.choose(load, f"k{i}") for i in range(6)]
    assert picks == ["a", "b", "c", "a", "b", "c"]


def test_least_loaded_picks_min() -> None:
    lb = LeastLoadedBalancer()
    assert lb.choose({"a": 5, "b": 1, "c": 3}, "k") == "b"


def test_power_of_two_picks_less_loaded_of_two() -> None:
    p2 = PowerOfTwoBalancer()
    choice = p2.choose({"a": 10, "b": 0, "c": 10, "d": 0}, "some-key")
    assert choice in {"b", "d"}  # one of the unloaded pair


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_health_checker_trips_and_recovers() -> None:
    clk = _Clock()
    hc = HealthChecker(["a", "b"], failure_threshold=2, reset_timeout=5.0, clock=clk)
    hc.record_failure("a")
    assert hc.is_healthy("a")
    hc.record_failure("a")  # trips
    assert not hc.is_healthy("a") and hc.healthy() == ["b"]
    clk.t = 5.0
    hc.record_success("a")
    assert hc.is_healthy("a")


def test_retrying_router_hedges_to_backup() -> None:
    router = ModelRouter([Backend("a"), Backend("b")])
    health = HealthChecker(["a", "b"], failure_threshold=1)
    rr = RetryingRouter(router, health, max_attempts=1)
    calls: list[str] = []

    def fn(backend: str) -> str:
        calls.append(backend)
        if len(calls) == 1:
            raise TransportError("primary down")
        return f"ok:{backend}"

    result = rr.call("req-1", fn)
    assert result.startswith("ok:") and len(calls) == 2  # primary failed, hedged to backup


def test_retrying_router_succeeds_on_primary() -> None:
    router = ModelRouter([Backend("a"), Backend("b")])
    health = HealthChecker(["a", "b"])
    rr = RetryingRouter(router, health)
    assert rr.call("k", lambda b: f"ok:{b}").startswith("ok:")
