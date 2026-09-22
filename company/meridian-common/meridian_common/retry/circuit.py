"""A circuit breaker and a retry driver.

The :class:`CircuitBreaker` trips OPEN after ``failure_threshold`` consecutive failures and short-circuits
calls
until a ``reset_timeout`` elapses, then allows a single HALF_OPEN trial: success closes it, failure
re-opens it.
:func:`retry_call` drives a callable through a backoff policy, honoring an optional deadline and a breaker,
and
raises :class:`RetryExhausted` (or :class:`CircuitOpenError`) when it gives up.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum

from meridian_common.errors import CircuitOpenError, MeridianError, RetryExhausted
from meridian_common.retry.backoff import BackoffPolicy, ExponentialBackoff
from meridian_common.retry.deadline import Deadline


class CircuitState(Enum):
    """The three states of a circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Trips open after consecutive failures; allows a trial call after a cooldown."""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Hold the failure threshold, the cooldown, and the clock."""
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self._threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._state = CircuitState.CLOSED

    @property
    def state(self) -> CircuitState:
        """The current state, transitioning OPEN -> HALF_OPEN once the cooldown has elapsed."""
        if self._state is CircuitState.OPEN and self._opened_at is not None:
            if self._clock() - self._opened_at >= self._reset_timeout:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def allow(self) -> bool:
        """Whether a call may proceed now (True in CLOSED and HALF_OPEN, False in OPEN)."""
        return self.state is not CircuitState.OPEN

    def on_success(self) -> None:
        """Record a success: close the breaker and clear the failure count."""
        self._failures = 0
        self._opened_at = None
        self._state = CircuitState.CLOSED

    def on_failure(self) -> None:
        """Record a failure: trip open at the threshold, or re-open from a failed HALF_OPEN trial."""
        if self.state is CircuitState.HALF_OPEN:
            self._trip()
            return
        self._failures += 1
        if self._failures >= self._threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self._clock()


def retry_call[T](
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    backoff: BackoffPolicy | None = None,
    deadline: Deadline | None = None,
    breaker: CircuitBreaker | None = None,
    sleep: Callable[[float], None] = time.sleep,
    retry_on: tuple[type[BaseException], ...] = (MeridianError,),
) -> T:
    """Call ``fn`` with retries under a backoff policy, an optional deadline, and an optional breaker.

    Raises :class:`CircuitOpenError` if the breaker is open, :class:`RetryExhausted` if every attempt fails,
    or :class:`~meridian_common.errors.DeadlineExceeded` if the deadline passes between attempts.
    """
    policy = backoff if backoff is not None else ExponentialBackoff()
    last: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        if breaker is not None and not breaker.allow():
            raise CircuitOpenError("circuit is open", attempts=attempt - 1)
        if deadline is not None:
            deadline.check()
        try:
            result = fn()
        except retry_on as exc:
            last = exc
            if breaker is not None:
                breaker.on_failure()
            if attempt >= max_attempts:
                break
            wait = policy.delay(attempt)
            if deadline is not None:
                wait = min(wait, deadline.remaining())
            sleep(wait)
        else:
            if breaker is not None:
                breaker.on_success()
            return result
    raise RetryExhausted("all retry attempts failed", attempts=max_attempts, last_error=str(last))
