"""A routing facade that retries and hedges a call across backends.

Wraps a :class:`~meridian_serving.router.router.ModelRouter` with a :class:`meridian_common.retry.Retryer`: a
call routes to a backend and, on a transport failure, retries under the policy; if the primary path is
exhausted it hedges to a second backend. Health is tracked so a failing backend is taken out of rotation. This
is where common's retry primitives meet serving's routing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from meridian_common.errors import TransportError
from meridian_common.retry import FixedBackoff, Retryer, RetryPolicy
from meridian_serving.router.health import HealthChecker
from meridian_serving.router.router import ModelRouter

T = TypeVar("T")


class RetryingRouter:
    """Routes a call to a healthy backend, retrying on failure and hedging to a second backend."""

    def __init__(
        self,
        router: ModelRouter,
        health: HealthChecker,
        *,
        max_attempts: int = 3,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        """Wire the underlying router, the health checker, and a retry policy."""
        self._router = router
        self._health = health
        policy = RetryPolicy(max_attempts=max_attempts, backoff=FixedBackoff(0.0), retry_on=(TransportError,))
        self._retryer = Retryer(policy, sleep=sleep if sleep is not None else (lambda _s: None))

    def call(self, key: str, fn: Callable[[str], T]) -> T:
        """Route ``key`` to a backend and invoke ``fn(backend)``, retrying and then hedging on failure.

        On each failure the chosen backend is recorded as unhealthy so a subsequent route avoids it; the
        underlying error propagates only if both the primary retries and the hedge backup fail.
        """
        primary = self._route(key)

        def _primary() -> T:
            try:
                result = fn(primary)
            except TransportError:
                self._health.record_failure(primary)
                raise
            self._health.record_success(primary)
            return result

        def _backup() -> T:
            backup = self._route(key + "#hedge")
            try:
                result = fn(backup)
            except TransportError:
                self._health.record_failure(backup)
                raise
            self._health.record_success(backup)
            return result

        return self._retryer.hedge(_primary, _backup, after=0.0)

    def _route(self, key: str) -> str:
        healthy = set(self._health.healthy())
        for salt in range(4):
            candidate = self._router.route(f"{key}:{salt}")
            if candidate in healthy or not healthy:
                return candidate
        return sorted(healthy)[0] if healthy else self._router.route(key)
