"""Backoff policies for retries: fixed, exponential, and jittered.

A :class:`BackoffPolicy` maps a 1-indexed attempt number to a delay in seconds. Exponential backoff doubles
per attempt up to a cap. Jittered backoff spreads retries with a random component so a fleet of callers does
not hammer a struggling dependency in lockstep.
"""

from __future__ import annotations

import random
from typing import Protocol


class BackoffPolicy(Protocol):
    """Maps a 1-indexed attempt number to a non-negative delay in seconds."""

    def delay(self, attempt: int) -> float:
        """The delay in seconds before ``attempt`` (attempt 1 is the first retry)."""
        ...


class FixedBackoff:
    """A constant delay between attempts."""

    def __init__(self, seconds: float) -> None:
        """Hold the constant delay."""
        self._seconds = seconds

    def delay(self, attempt: int) -> float:
        """Return the constant delay for any attempt."""
        return self._seconds


class ExponentialBackoff:
    """Delay doubles per attempt from ``base`` up to ``cap`` (attempt 1 -> base)."""

    def __init__(self, base: float = 0.1, cap: float = 30.0) -> None:
        """Hold the base delay and the maximum cap."""
        if base <= 0:
            raise ValueError("base must be positive")
        self._base = base
        self._cap = cap

    def _uncapped(self, attempt: int) -> float:
        return self._base * float(2 ** max(0, attempt - 1))

    def delay(self, attempt: int) -> float:
        """The exponential delay for ``attempt``, capped at ``cap``."""
        return min(self._cap, self._uncapped(attempt))


class JitteredBackoff:
    """Equal-jitter backoff over the exponential term, capped at ``cap``.

    The exponential term for an attempt is ``temp = min(cap, base*2^(attempt-1))``; the returned delay applies
    jitter around it so retries spread out. The RNG is injectable for deterministic tests.
    """

    def __init__(self, base: float = 0.1, cap: float = 30.0, *, rng: random.Random | None = None) -> None:
        """Hold the exponential base/cap and an injectable RNG (deterministic when seeded)."""
        self._exp = ExponentialBackoff(base, cap)
        self._rng = rng if rng is not None else random.Random()

    def delay(self, attempt: int) -> float:
        """A jittered delay for ``attempt``, bounded above by the exponential term."""
        temp = self._exp.delay(attempt)
        return self._rng.uniform(0, temp)


class DecorrelatedJitterBackoff:
    """Decorrelated-jitter backoff (AWS style): the next delay is random in ``[base, prev*3]``, capped.

    Unlike equal jitter, this policy is stateful across a retry sequence: each delay grows from the previous
    one while keeping a floor of ``base``. ``reset`` starts a new sequence. It is used where a wider spread of
    retry timings is preferred to a tight bound.
    """

    def __init__(self, base: float = 0.1, cap: float = 30.0, *, rng: random.Random | None = None) -> None:
        """Hold the floor, the cap, and an injectable RNG; start a fresh sequence."""
        if base <= 0:
            raise ValueError("base must be positive")
        self._base = base
        self._cap = cap
        self._rng = rng if rng is not None else random.Random()
        self._prev = base

    def reset(self) -> None:
        """Restart the decorrelated sequence from ``base``."""
        self._prev = self._base

    def delay(self, attempt: int) -> float:
        """The next decorrelated delay, in ``[base, min(cap, prev*3)]`` (attempt is advisory here)."""
        upper = min(self._cap, self._prev * 3)
        nxt = self._rng.uniform(self._base, max(self._base, upper))
        self._prev = nxt
        return nxt


def delays(policy: BackoffPolicy, attempts: int) -> list[float]:
    """The delay sequence a ``policy`` would produce for attempts ``1..attempts`` (for planning/tests)."""
    return [policy.delay(a) for a in range(1, attempts + 1)]
