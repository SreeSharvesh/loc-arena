"""Deadline propagation across nested calls.

A :class:`Deadline` marks an absolute expiry. A top-level request sets a budget, and each downstream call
derives a child deadline from a per-call timeout so the whole call tree stays inside the request's budget.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from meridian_common.errors import DeadlineExceeded


class Deadline:
    """An absolute expiry time; ``remaining`` and ``expired`` read the injected clock."""

    def __init__(self, expiry: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        """Hold the absolute expiry and the clock used to measure remaining time."""
        self._expiry = expiry
        self._clock = clock

    @classmethod
    def after(cls, timeout: float, *, clock: Callable[[], float] = time.monotonic) -> Deadline:
        """A deadline ``timeout`` seconds from now (by the given clock)."""
        return cls(clock() + timeout, clock=clock)

    @property
    def expiry(self) -> float:
        """The absolute expiry time."""
        return self._expiry

    def remaining(self) -> float:
        """Seconds left before expiry (0.0 once expired)."""
        return max(0.0, self._expiry - self._clock())

    def expired(self) -> bool:
        """Whether the deadline has passed."""
        return self._clock() >= self._expiry

    def check(self) -> None:
        """Raise :class:`DeadlineExceeded` if the deadline has already passed."""
        if self.expired():
            raise DeadlineExceeded("deadline already exceeded", remaining=0.0)

    def child(self, timeout: float) -> Deadline:
        """Derive a child deadline for a downstream call with its own ``timeout``."""
        return Deadline(self._clock() + timeout, clock=self._clock)
