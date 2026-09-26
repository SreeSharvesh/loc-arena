"""Per-backend health tracking with a circuit breaker.

Each backend gets a :class:`meridian_common.retry.CircuitBreaker`. Consecutive failures trip the breaker open,
which marks the backend unhealthy until a cooldown elapses and a probe succeeds. The health checker
exposes the
current healthy set so the router only routes to backends that are up, and records success/failure so a
flapping
backend is taken out of rotation.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from meridian_common.retry import CircuitBreaker, CircuitState


class HealthChecker:
    """Tracks per-backend health via circuit breakers over a shared clock."""

    def __init__(
        self,
        backends: list[str],
        *,
        failure_threshold: int = 3,
        reset_timeout: float = 5.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Create a breaker per backend with the given threshold, cooldown, and clock."""
        the_clock = clock if clock is not None else time.monotonic
        self._breakers = {
            name: CircuitBreaker(
                failure_threshold=failure_threshold,
                reset_timeout=reset_timeout,
                clock=the_clock,
            )
            for name in backends
        }

    def record_success(self, backend: str) -> None:
        """Record a successful call to ``backend`` (closes its breaker)."""
        if backend in self._breakers:
            self._breakers[backend].on_success()

    def record_failure(self, backend: str) -> None:
        """Record a failed call to ``backend`` (may trip its breaker)."""
        if backend in self._breakers:
            self._breakers[backend].on_failure()

    def is_healthy(self, backend: str) -> bool:
        """Whether ``backend``'s breaker currently allows traffic."""
        breaker = self._breakers.get(backend)
        return breaker is not None and breaker.allow()

    def healthy(self) -> list[str]:
        """The sorted set of backends currently allowing traffic."""
        return sorted(name for name, b in self._breakers.items() if b.allow())

    def state(self, backend: str) -> CircuitState:
        """The circuit state of ``backend``'s breaker."""
        return self._breakers[backend].state
