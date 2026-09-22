"""Serving observability: bind serving events to the shared metrics registry.

A thin collector that records the events the serving stack emits (admissions, rejections, batch sizes, decode
latencies, cache hits, evictions) into a :class:`meridian_common.metrics.MetricsRegistry`, and exports a
deterministic snapshot for the platform's metrics scrape. It is the single place serving names its metrics, so
the router, scheduler, and cache all report through one registry.
"""

from __future__ import annotations

from typing import Any

from meridian_common.metrics import MetricsRegistry

_LATENCY_BUCKETS = [0.005, 0.01, 0.05, 0.1, 0.5, 1.0]


class ServingObservability:
    """Records serving metrics into a shared registry and exports a snapshot."""

    def __init__(self, registry: MetricsRegistry | None = None) -> None:
        """Hold (or create) the metrics registry the serving stack reports through."""
        self._metrics = registry if registry is not None else MetricsRegistry()

    @property
    def registry(self) -> MetricsRegistry:
        """The underlying metrics registry."""
        return self._metrics

    def admitted(self, n: int = 1) -> None:
        """Count ``n`` admitted requests."""
        self._metrics.counter("serving_admitted_total").inc(n)

    def rejected(self, reason: str) -> None:
        """Count one rejected request under its reason label."""
        self._metrics.counter("serving_rejected_total", {"reason": reason}).inc()

    def observe_batch(self, size: int) -> None:
        """Record a formed batch's size into the batch-size histogram."""
        self._metrics.histogram("serving_batch_size", [1, 2, 4, 8, 16, 32, 64]).observe(size)

    def observe_latency(self, seconds: float) -> None:
        """Record a decode-step latency."""
        self._metrics.histogram("serving_decode_latency_seconds", _LATENCY_BUCKETS).observe(seconds)

    def cache_event(self, hit: bool) -> None:
        """Count a cache hit or miss."""
        self._metrics.counter("serving_cache_total", {"result": "hit" if hit else "miss"}).inc()

    def eviction(self, n: int = 1) -> None:
        """Count ``n`` cache evictions."""
        self._metrics.counter("serving_cache_evictions_total").inc(n)

    def export(self) -> dict[str, Any]:
        """A deterministic snapshot of every serving metric."""
        return self._metrics.collect()
