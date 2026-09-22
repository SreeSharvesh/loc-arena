"""Per-tenant token-bucket rate limiting.

Each tenant gets a token bucket that refills at a steady rate up to a burst capacity. A request of ``cost``
tokens is admitted only if the bucket has the tokens, which smooths bursts while allowing a configurable burst
size. The bucket is deterministic given the clock, so tests drive it with a fake clock.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class TokenBucketLimiter:
    """Refilling token buckets per tenant with a shared rate and burst capacity."""

    def __init__(
        self,
        *,
        refill_per_second: float,
        burst: float,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Hold the refill rate, the burst capacity, and the clock."""
        if refill_per_second <= 0 or burst <= 0:
            raise ValueError("refill_per_second and burst must be positive")
        self._rate = refill_per_second
        self._burst = burst
        self._clock = clock if clock is not None else time.monotonic
        self._buckets: dict[str, _Bucket] = {}

    def _bucket(self, tenant: str) -> _Bucket:
        now = self._clock()
        bucket = self._buckets.get(tenant)
        if bucket is None:
            bucket = _Bucket(tokens=self._burst, last_refill=now)
            self._buckets[tenant] = bucket
            return bucket
        elapsed = now - bucket.last_refill
        bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._rate)
        bucket.last_refill = now
        return bucket

    def allow(self, tenant: str, cost: float = 1.0) -> bool:
        """Whether ``tenant`` may spend ``cost`` tokens now; deduct them if so."""
        bucket = self._bucket(tenant)
        if bucket.tokens >= cost:
            bucket.tokens -= cost
            return True
        return False

    def available(self, tenant: str) -> float:
        """The tokens currently available to ``tenant`` (after a refill)."""
        return self._bucket(tenant).tokens
