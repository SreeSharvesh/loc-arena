"""A model router with weighted, deterministic backend selection and a metrics exporter.

Backends are registered with a weight and a health flag. ``route`` maps a routing key to a healthy backend
deterministically (the same key always lands on the same backend for a given healthy set and weighting), so
retries are sticky and caches stay warm. The router records per-backend request counts and latencies into a
:class:`meridian_common.metrics.MetricsRegistry` and exposes a snapshot.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from meridian_common.metrics import MetricsRegistry
from meridian_serving.errors import RoutingError


@dataclass(frozen=True)
class Backend:
    """A model backend: its name, routing weight, and health flag."""

    name: str
    weight: int = 1
    healthy: bool = True


class ModelRouter:
    """Routes keys to weighted, healthy backends and records routing metrics."""

    def __init__(self, backends: list[Backend], *, registry: MetricsRegistry | None = None) -> None:
        """Hold the backends (by name) and a metrics registry."""
        if not backends:
            raise ValueError("at least one backend is required")
        self._backends: dict[str, Backend] = {b.name: b for b in backends}
        self._metrics = registry if registry is not None else MetricsRegistry()

    @property
    def metrics(self) -> MetricsRegistry:
        """The metrics registry the router records into."""
        return self._metrics

    def set_health(self, name: str, healthy: bool) -> None:
        """Mark backend ``name`` healthy or unhealthy."""
        if name not in self._backends:
            raise RoutingError(f"unknown backend {name!r}", backend=name)
        b = self._backends[name]
        self._backends[name] = Backend(b.name, b.weight, healthy)

    def _healthy_ring(self) -> list[str]:
        ring: list[str] = []
        for name in sorted(self._backends):
            b = self._backends[name]
            if b.healthy and b.weight > 0:
                ring.extend([name] * b.weight)
        return ring

    def route(self, key: str) -> str:
        """Select a healthy backend for ``key`` deterministically by weight; raise if none are healthy."""
        ring = self._healthy_ring()
        if not ring:
            raise RoutingError("no healthy backend available")
        bucket = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:4], "big") % len(ring)
        chosen = ring[bucket]
        self._metrics.counter("router_requests_total", {"backend": chosen}).inc()
        return chosen

    def record_latency(self, backend: str, seconds: float) -> None:
        """Record a request latency for ``backend`` into the latency histogram."""
        self._metrics.histogram(
            "router_latency_seconds",
            [0.01, 0.05, 0.1, 0.5, 1.0],
            {"backend": backend},
        ).observe(seconds)

    def export(self) -> dict[str, Any]:
        """A deterministic snapshot of the router's metrics for the exporter."""
        return self._metrics.collect()
