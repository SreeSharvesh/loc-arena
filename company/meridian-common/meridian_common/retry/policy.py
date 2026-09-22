"""A composed retry policy and a reusable :class:`Retryer`.

Where :func:`~meridian_common.retry.circuit.retry_call` is the one-shot driver, a :class:`RetryPolicy` bundles
the knobs (attempts, backoff, breaker, deadline timeout, which errors retry) so a service configures retries
once and applies them everywhere. :class:`Retryer` binds a policy to a clock/sleep and exposes ``run`` and a
``hedge`` that races a primary against a delayed backup, returning the first success.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from meridian_common.errors import MeridianError, RetryExhausted
from meridian_common.retry.backoff import BackoffPolicy, ExponentialBackoff
from meridian_common.retry.circuit import CircuitBreaker, retry_call
from meridian_common.retry.deadline import Deadline


@dataclass(frozen=True)
class RetryPolicy:
    """The configured retry knobs for a service call."""

    max_attempts: int = 3
    timeout: float | None = None
    backoff: BackoffPolicy = field(default_factory=ExponentialBackoff)
    retry_on: tuple[type[BaseException], ...] = (MeridianError,)

    def __post_init__(self) -> None:
        """Validate the attempt count."""
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")


class Retryer:
    """Applies a :class:`RetryPolicy` (optionally with a shared circuit breaker) to callables."""

    def __init__(
        self,
        policy: RetryPolicy | None = None,
        *,
        breaker: CircuitBreaker | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Wire the retryer to its policy, an optional shared breaker, and the clock/sleep."""
        self._policy = policy if policy is not None else RetryPolicy()
        self._breaker = breaker
        self._clock = clock
        self._sleep = sleep

    def _deadline(self) -> Deadline | None:
        if self._policy.timeout is None:
            return None
        return Deadline.after(self._policy.timeout, clock=self._clock)

    def run[T](self, fn: Callable[[], T]) -> T:
        """Run ``fn`` under the policy; on give-up raise :class:`RetryExhausted` (or breaker/deadline)."""
        return retry_call(
            fn,
            max_attempts=self._policy.max_attempts,
            backoff=self._policy.backoff,
            deadline=self._deadline(),
            breaker=self._breaker,
            sleep=self._sleep,
            retry_on=self._policy.retry_on,
        )

    def hedge[T](self, primary: Callable[[], T], backup: Callable[[], T], *, after: float) -> T:
        """Try ``primary``; if it fails under the retry policy, wait ``after`` and try ``backup`` once.

        A pragmatic, single-process hedge: it does not run the two concurrently (this bus is synchronous), but
        it bounds tail latency by falling back to a second replica when the primary path is exhausted.
        """
        try:
            return self.run(primary)
        except RetryExhausted:
            self._sleep(after)
            return backup()
