"""The Meridian typed error hierarchy.

Every Meridian repo raises from this hierarchy so callers can catch by category (configuration, transport,
timeout, validation, capacity) rather than by string matching. ``MeridianError`` carries a stable ``code`` and
a ``context`` mapping so structured logging and the event bus can serialize an error without losing detail.
"""

from __future__ import annotations

from typing import Any


class MeridianError(Exception):
    """Base class for every Meridian error: a stable ``code`` plus a ``context`` mapping.

    ``code`` is a short machine-stable token (e.g. ``"config.missing"``) that never changes across releases,
    so alerts and dashboards key on it. ``context`` holds structured detail for logging and serialization.
    """

    code: str = "meridian.error"

    def __init__(self, message: str, *, code: str | None = None, **context: Any) -> None:
        """Build the error with a human message, an optional code override, and structured context."""
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.context: dict[str, Any] = dict(context)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the error to a JSON-friendly mapping (used by logging and the event bus)."""
        return {"code": self.code, "message": self.message, "context": dict(self.context)}

    def __str__(self) -> str:
        """Render as ``<code>: <message>`` for logs."""
        return f"{self.code}: {self.message}"


class ConfigError(MeridianError):
    """A configuration value was missing, malformed, or failed schema validation."""

    code = "config.error"


class ValidationError(MeridianError):
    """A payload failed schema or envelope validation (serde, config schema, RBAC input)."""

    code = "validation.error"


class TransportError(MeridianError):
    """A transport call (jobclient, authclient, event delivery) failed at the wire level."""

    code = "transport.error"


class TimeoutError(MeridianError):  # noqa: A001, N818 - domain name mirrors grpc-style status names
    """An operation exceeded its deadline (retry/deadline propagation, client calls)."""

    code = "timeout.error"


class DeadlineExceeded(TimeoutError):  # noqa: N818 - grpc-style status name
    """A deadline propagated from a parent context was already exhausted before the call could start."""

    code = "timeout.deadline_exceeded"


class CircuitOpenError(TransportError):
    """A circuit breaker is open, so the call was short-circuited instead of dialed."""

    code = "transport.circuit_open"


class RetryExhausted(TransportError):  # noqa: N818 - reads as a state, not an Error type
    """Every retry attempt failed; carries the last underlying error in ``context['last_error']``."""

    code = "transport.retry_exhausted"


class AuthError(MeridianError):
    """Credential issuance, refresh, or validation failed."""

    code = "auth.error"


class CapacityError(MeridianError):
    """A queue, quota, or admission limit was exceeded."""

    code = "capacity.error"


class NotFoundError(MeridianError):
    """A referenced entity (job, schema, feature flag, registry key) does not exist."""

    code = "not_found.error"
