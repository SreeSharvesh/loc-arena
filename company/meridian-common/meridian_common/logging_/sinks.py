"""Composable log sinks: fan-out, sampling, and rate limiting.

A sink is a callable that consumes one structured record. :class:`MultiSink` fans a record out to several
sinks; :class:`SamplingSink` forwards a deterministic fraction of records (always keeping warnings and above);
:class:`RateLimitedSink` drops records once a per-window budget is exceeded and counts the drops. They
compose,
so a service can, say, sample debug traffic while always keeping errors and never flooding the shipper.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Any

Sink = Callable[[dict[str, Any]], None]

_LEVEL_RANK = {"debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50}


class MultiSink:
    """Fans each record out to several downstream sinks in order."""

    def __init__(self, *sinks: Sink) -> None:
        """Hold the downstream sinks."""
        self._sinks = list(sinks)

    def __call__(self, record: dict[str, Any]) -> None:
        """Deliver ``record`` to every downstream sink."""
        for sink in self._sinks:
            sink(record)


class SamplingSink:
    """Forwards a deterministic fraction of records; always keeps ``warning`` and above."""

    def __init__(self, downstream: Sink, *, sample_rate: float, keep_from: str = "warning") -> None:
        """Wire the downstream sink, the 0..1 sample rate, and the minimum always-kept level."""
        if not 0.0 <= sample_rate <= 1.0:
            raise ValueError("sample_rate must be in [0, 1]")
        self._downstream = downstream
        self._rate = sample_rate
        self._keep_rank = _LEVEL_RANK.get(keep_from, 30)
        self._seen = 0

    def __call__(self, record: dict[str, Any]) -> None:
        """Forward the record if it clears the level floor or falls in the sampled fraction."""
        self._seen += 1
        rank = _LEVEL_RANK.get(str(record.get("level", "info")), 20)
        if rank >= self._keep_rank:
            self._downstream(record)
            return
        # deterministic sampling: keep 1 in N by counter position (no RNG, reproducible)
        if self._rate <= 0.0:
            return
        stride = max(1, round(1 / self._rate))
        if self._seen % stride == 0:
            self._downstream(record)


class RateLimitedSink:
    """Forwards up to ``max_per_window`` records per sliding window; counts what it drops."""

    def __init__(
        self,
        downstream: Sink,
        *,
        max_per_window: int,
        window_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Wire the downstream sink, the per-window budget, the window length, and the clock."""
        if max_per_window < 0:
            raise ValueError("max_per_window must be non-negative")
        self._downstream = downstream
        self._max = max_per_window
        self._window = window_seconds
        self._clock = clock
        self._times: deque[float] = deque()
        self.dropped = 0

    def __call__(self, record: dict[str, Any]) -> None:
        """Forward the record if under budget, else drop it and increment the drop counter."""
        now = self._clock()
        cutoff = now - self._window
        while self._times and self._times[0] < cutoff:
            self._times.popleft()
        if len(self._times) >= self._max:
            self.dropped += 1
            return
        self._times.append(now)
        self._downstream(record)
