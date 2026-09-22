"""meridian-common: the shared foundation every Meridian service depends on.

Owns configuration (layered loader, schema validation, feature flags), structured logging (redaction,
correlation ids), retry primitives (backoff, circuit breaker, deadline propagation), the serde layer
(canonical
JSON, a versioned schema registry, message envelopes), an in-process event bus, metric primitives with a
registry, typed clients to the job and identity services, and the typed error hierarchy. It depends on nothing
and is depended on by every other Meridian repo.
"""

from __future__ import annotations

__version__ = "0.4.0"

from meridian_common import (
    authclient,
    config,
    errors,
    eventbus,
    jobclient,
    logging_,
    metrics,
    retry,
    serde,
)

__all__ = [
    "authclient",
    "config",
    "errors",
    "eventbus",
    "jobclient",
    "logging_",
    "metrics",
    "retry",
    "serde",
]
