"""Contract tests tracking two open backlog tickets against meridian-common.

Each is a terse ``strict`` xfail tied to its ticket: it asserts the documented behavior and currently fails,
so it is an expected failure now and turns into a hard failure the moment the ticket is resolved (prompting
the marker's removal). The mechanism is intentionally not described here.
"""

from __future__ import annotations

import random

import pytest

from meridian_common.retry import Deadline, JitteredBackoff


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.mark.xfail(strict=True, reason="MER-COMMON-14 (open)")
def test_jittered_backoff_guarantees_equal_jitter_floor() -> None:
    # for attempt 3 the exponential term is base*2^2 = 4.0; equal jitter must keep the delay at or above 2.0
    b = JitteredBackoff(base=1.0, cap=100.0, rng=random.Random(0))
    samples = [b.delay(3) for _ in range(200)]
    assert min(samples) >= 2.0


@pytest.mark.xfail(strict=True, reason="MER-COMMON-22 (open)")
def test_child_deadline_never_outlives_parent() -> None:
    clk = _Clock()
    parent = Deadline.after(1.0, clock=clk)
    child = parent.child(10.0)
    assert child.remaining() <= parent.remaining()
