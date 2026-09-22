"""Metric primitives: counters, gauges, and histograms.

Deterministic, dependency-free instruments for the platform. A :class:`Counter` only increases; a
:class:`Gauge`
is set to an arbitrary value; a :class:`Histogram` records observations into fixed buckets and reports count,
sum, and quantiles. Each instrument carries a name and immutable labels so the registry can key on the pair.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field


def _label_key(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(labels.items()))


@dataclass(frozen=True)
class InstrumentId:
    """A metric identity: its name plus a sorted label tuple (hashable, so it keys the registry)."""

    name: str
    labels: tuple[tuple[str, str], ...] = ()

    @staticmethod
    def of(name: str, labels: dict[str, str] | None = None) -> InstrumentId:
        """Build an id from a name and an optional label mapping."""
        return InstrumentId(name=name, labels=_label_key(labels or {}))


class Counter:
    """A monotonically increasing counter."""

    def __init__(self, name: str, labels: dict[str, str] | None = None) -> None:
        """Name the counter and hold its immutable labels."""
        self.id = InstrumentId.of(name, labels)
        self._value = 0.0

    def inc(self, amount: float = 1.0) -> None:
        """Increase the counter by ``amount`` (must be non-negative)."""
        if amount < 0:
            raise ValueError("counter increment must be non-negative")
        self._value += amount

    @property
    def value(self) -> float:
        """The current counter value."""
        return self._value


class Gauge:
    """A value that can be set up or down."""

    def __init__(self, name: str, labels: dict[str, str] | None = None) -> None:
        """Name the gauge and hold its immutable labels."""
        self.id = InstrumentId.of(name, labels)
        self._value = 0.0

    def set(self, value: float) -> None:
        """Set the gauge to ``value``."""
        self._value = value

    def inc(self, amount: float = 1.0) -> None:
        """Add ``amount`` (possibly negative) to the gauge."""
        self._value += amount

    @property
    def value(self) -> float:
        """The current gauge value."""
        return self._value


@dataclass
class HistogramSnapshot:
    """A point-in-time view of a histogram: count, sum, and per-bucket cumulative counts."""

    count: int
    sum: float
    buckets: list[tuple[float, int]] = field(default_factory=list)


class Histogram:
    """Records observations into fixed upper-bound buckets; reports count, sum, and quantiles."""

    def __init__(self, name: str, bounds: list[float], labels: dict[str, str] | None = None) -> None:
        """Name the histogram, fix its sorted bucket upper bounds, and hold its labels."""
        if not bounds or any(b <= 0 for b in bounds):
            raise ValueError("histogram bounds must be a non-empty list of positive numbers")
        self.id = InstrumentId.of(name, labels)
        self._bounds = sorted(bounds)
        self._counts = [0] * len(self._bounds)
        self._inf = 0  # observations above the last bound
        self._sum = 0.0
        self._n = 0
        self._samples: list[float] = []

    def observe(self, value: float) -> None:
        """Record one observation."""
        self._n += 1
        self._sum += value
        self._samples.append(value)
        idx = bisect.bisect_left(self._bounds, value)
        if idx >= len(self._bounds):
            self._inf += 1
        else:
            self._counts[idx] += 1

    def quantile(self, q: float) -> float:
        """The ``q`` quantile (0..1) over the recorded samples (nearest-rank); 0.0 if empty."""
        if not 0.0 <= q <= 1.0:
            raise ValueError("quantile must be in [0, 1]")
        if not self._samples:
            return 0.0
        ordered = sorted(self._samples)
        rank = max(0, min(len(ordered) - 1, round(q * (len(ordered) - 1))))
        return ordered[rank]

    def snapshot(self) -> HistogramSnapshot:
        """A cumulative snapshot: count, sum, and cumulative bucket counts."""
        cumulative: list[tuple[float, int]] = []
        running = 0
        for bound, count in zip(self._bounds, self._counts, strict=True):
            running += count
            cumulative.append((bound, running))
        cumulative.append((float("inf"), running + self._inf))
        return HistogramSnapshot(count=self._n, sum=self._sum, buckets=cumulative)
