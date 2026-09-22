from __future__ import annotations

import random

import pytest

from meridian_common.errors import (
    CircuitOpenError,
    DeadlineExceeded,
    RetryExhausted,
    TransportError,
)
from meridian_common.retry import (
    CircuitBreaker,
    Deadline,
    ExponentialBackoff,
    FixedBackoff,
    JitteredBackoff,
    retry_call,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _state(cb: CircuitBreaker) -> str:
    # read the (recomputed) property fresh each call so mypy does not narrow it across state changes
    return cb.state.value


def test_exponential_backoff_doubles_and_caps() -> None:
    b = ExponentialBackoff(base=0.1, cap=1.0)
    assert b.delay(1) == pytest.approx(0.1)
    assert b.delay(2) == pytest.approx(0.2)
    assert b.delay(3) == pytest.approx(0.4)
    assert b.delay(10) == pytest.approx(1.0)  # capped


def test_fixed_backoff() -> None:
    assert FixedBackoff(0.5).delay(7) == 0.5


def test_jittered_backoff_within_cap() -> None:
    b = JitteredBackoff(base=0.1, cap=1.0, rng=random.Random(0))
    for attempt in range(1, 6):
        d = b.delay(attempt)
        assert 0.0 <= d <= 1.0  # never exceeds the exponential term / cap


def test_retry_succeeds_after_transient_failures() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransportError("temporary")
        return "ok"

    out = retry_call(flaky, max_attempts=5, backoff=FixedBackoff(0.0), sleep=lambda _s: None)
    assert out == "ok" and calls["n"] == 3


def test_retry_exhausts_and_raises() -> None:
    def always() -> str:
        raise TransportError("nope")

    with pytest.raises(RetryExhausted):
        retry_call(always, max_attempts=3, backoff=FixedBackoff(0.0), sleep=lambda _s: None)


def test_deadline_check_raises_when_passed() -> None:
    def always() -> str:
        raise TransportError("nope")

    clk = _Clock()
    deadline = Deadline.after(0.0, clock=clk)
    clk.t = 1.0  # already past
    with pytest.raises(DeadlineExceeded):
        retry_call(
            always, max_attempts=5, backoff=FixedBackoff(0.0), deadline=deadline, sleep=lambda _s: None
        )


def test_circuit_breaker_opens_then_half_opens_then_closes() -> None:
    clk = _Clock()
    cb = CircuitBreaker(failure_threshold=2, reset_timeout=10.0, clock=clk)
    cb.on_failure()
    assert _state(cb) == "closed" and cb.allow()
    cb.on_failure()
    assert _state(cb) == "open" and not cb.allow()
    clk.t = 10.0
    assert _state(cb) == "half_open" and cb.allow()  # cooldown elapsed
    cb.on_success()
    assert _state(cb) == "closed"


def test_open_circuit_short_circuits_retry() -> None:
    clk = _Clock()
    cb = CircuitBreaker(failure_threshold=1, reset_timeout=100.0, clock=clk)
    cb.on_failure()  # trips open
    with pytest.raises(CircuitOpenError):
        retry_call(lambda: "x", max_attempts=3, breaker=cb, backoff=FixedBackoff(0.0), sleep=lambda _s: None)


def test_deadline_remaining_and_expired() -> None:
    clk = _Clock()
    d = Deadline.after(5.0, clock=clk)
    assert d.remaining() == pytest.approx(5.0) and not d.expired()
    clk.t = 5.0
    assert d.remaining() == 0.0 and d.expired()
