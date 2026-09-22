"""A metrics registry that owns instruments by identity and renders a snapshot.

Services register counters, gauges, and histograms through the registry so there is one place to enumerate and
export them. ``get_or_create`` is idempotent per ``(name, labels)`` so repeated registration returns the same
instrument, and ``collect`` renders a deterministic, sorted snapshot for the metrics exporter.
"""

from __future__ import annotations

from typing import Any

from meridian_common.errors import ValidationError
from meridian_common.metrics.primitives import Counter, Gauge, Histogram, InstrumentId


class MetricsRegistry:
    """Owns instruments keyed by :class:`~meridian_common.metrics.primitives.InstrumentId`."""

    def __init__(self) -> None:
        """Start empty."""
        self._counters: dict[InstrumentId, Counter] = {}
        self._gauges: dict[InstrumentId, Gauge] = {}
        self._histograms: dict[InstrumentId, Histogram] = {}

    def counter(self, name: str, labels: dict[str, str] | None = None) -> Counter:
        """Get or create a counter for ``(name, labels)``."""
        key = InstrumentId.of(name, labels)
        self._reject_conflict(key, self._gauges, self._histograms)
        return self._counters.setdefault(key, Counter(name, labels))

    def gauge(self, name: str, labels: dict[str, str] | None = None) -> Gauge:
        """Get or create a gauge for ``(name, labels)``."""
        key = InstrumentId.of(name, labels)
        self._reject_conflict(key, self._counters, self._histograms)
        return self._gauges.setdefault(key, Gauge(name, labels))

    def histogram(self, name: str, bounds: list[float], labels: dict[str, str] | None = None) -> Histogram:
        """Get or create a histogram for ``(name, labels)`` with the given bucket bounds."""
        key = InstrumentId.of(name, labels)
        self._reject_conflict(key, self._counters, self._gauges)
        return self._histograms.setdefault(key, Histogram(name, bounds, labels))

    @staticmethod
    def _reject_conflict(key: InstrumentId, *others: dict[InstrumentId, Any]) -> None:
        if any(key in other for other in others):
            raise ValidationError(
                f"metric {key.name!r} already registered as a different type", path=key.name
            )

    def collect(self) -> dict[str, Any]:
        """A deterministic, sorted snapshot of every instrument for export."""

        def _rows(items: dict[InstrumentId, Any], render: Any) -> list[dict[str, Any]]:
            rows = []
            for iid in sorted(items, key=lambda k: (k.name, k.labels)):
                rows.append({"name": iid.name, "labels": dict(iid.labels), **render(items[iid])})
            return rows

        return {
            "counters": _rows(self._counters, lambda c: {"value": c.value}),
            "gauges": _rows(self._gauges, lambda g: {"value": g.value}),
            "histograms": _rows(
                self._histograms,
                lambda h: {"count": h.snapshot().count, "sum": h.snapshot().sum},
            ),
        }
