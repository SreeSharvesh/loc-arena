"""Timing and rate instruments: a timer, a windowed rate counter, an EWMA, and a summary.

These build on the primitives for the common case of measuring durations and rates. :class:`Timer` is a
context
manager that observes elapsed time into a histogram; :class:`RateCounter` reports events-per-second over a
sliding window; :class:`Ewma` is an exponentially weighted moving average; :class:`Summary` tracks count, sum,
min, max, and mean without keeping every sample.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from meridian_common.metrics.primitives import Histogram


@contextmanager
def timed(histogram: Histogram, *, clock: Callable[[], float] = time.perf_counter) -> Iterator[None]:
    """Observe the wall-clock duration of the ``with`` block into ``histogram`` (seconds)."""
    start = clock()
    try:
        yield
    finally:
        histogram.observe(clock() - start)


class Timer:
    """A reusable timer that records each ``measure`` block's duration into a histogram."""

    def __init__(self, histogram: Histogram, *, clock: Callable[[], float] = time.perf_counter) -> None:
        """Wire the timer to the histogram it records into and its clock."""
        self._hist = histogram
        self._clock = clock

    @contextmanager
    def measure(self) -> Iterator[None]:
        """Time the ``with`` block and record its duration."""
        start = self._clock()
        try:
            yield
        finally:
            self._hist.observe(self._clock() - start)


class RateCounter:
    """Events-per-second over a sliding time window."""

    def __init__(self, window_seconds: float = 60.0, *, clock: Callable[[], float] = time.monotonic) -> None:
        """Hold the window length and the clock; start empty."""
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._window = window_seconds
        self._clock = clock
        self._events: deque[float] = deque()

    def mark(self, n: int = 1) -> None:
        """Record ``n`` events at the current time."""
        now = self._clock()
        for _ in range(n):
            self._events.append(now)
        self._evict(now)

    def _evict(self, now: float) -> None:
        cutoff = now - self._window
        while self._events and self._events[0] < cutoff:
            self._events.popleft()

    def rate(self) -> float:
        """The current events-per-second over the window."""
        now = self._clock()
        self._evict(now)
        return len(self._events) / self._window

    def count(self) -> int:
        """The number of events currently within the window."""
        self._evict(self._clock())
        return len(self._events)


class Ewma:
    """An exponentially weighted moving average with a configurable smoothing factor."""

    def __init__(self, alpha: float = 0.2) -> None:
        """Hold the smoothing factor ``alpha`` in (0, 1]; start with no value."""
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        self._alpha = alpha
        self._value: float | None = None

    def update(self, sample: float) -> float:
        """Fold ``sample`` into the average and return the new value."""
        if self._value is None:
            self._value = sample
        else:
            self._value = self._alpha * sample + (1 - self._alpha) * self._value
        return self._value

    @property
    def value(self) -> float:
        """The current average (0.0 before the first sample)."""
        return self._value if self._value is not None else 0.0


@dataclass
class Summary:
    """Running count, sum, min, max, and mean without retaining samples."""

    count: int = 0
    total: float = 0.0
    minimum: float = float("inf")
    maximum: float = float("-inf")

    def observe(self, value: float) -> None:
        """Fold one observation into the summary."""
        self.count += 1
        self.total += value
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)

    @property
    def mean(self) -> float:
        """The mean of the observations (0.0 if none)."""
        return self.total / self.count if self.count else 0.0
