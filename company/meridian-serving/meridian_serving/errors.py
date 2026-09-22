"""Serving-specific errors, extending the shared Meridian hierarchy.

Serving raises these so callers distinguish an admission rejection from a scheduler fault from a cache miss.
They all derive from :class:`meridian_common.errors.MeridianError`, so a generic handler still catches them.
"""

from __future__ import annotations

from meridian_common.errors import CapacityError, MeridianError, NotFoundError


class QueueFullError(CapacityError):
    """The admission queue is at capacity and cannot accept another request."""

    code = "serving.queue_full"


class SchedulerError(MeridianError):
    """The scheduler could not form or run a batch."""

    code = "serving.scheduler"


class CacheMiss(NotFoundError):  # noqa: N818 - reads as a state, not an Error type
    """A requested key is not resident in the cache."""

    code = "serving.cache_miss"


class AllocationError(CapacityError):
    """The paged allocator has no free blocks to satisfy a request."""

    code = "serving.allocation"


class RoutingError(NotFoundError):
    """No model backend matches the routing request."""

    code = "serving.routing"


class SamplerError(MeridianError):
    """A sampler was given invalid parameters or an empty candidate set."""

    code = "serving.sampler"
