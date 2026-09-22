"""Evalkit-specific errors, extending the shared Meridian hierarchy.

The eval platform raises these so callers can tell a harness fault from a metric fault from a runner,
store, leaderboard, or benchmark fault. They all derive from
:class:`meridian_common.errors.MeridianError`, so a generic handler still catches them, while the stable
``code`` keys alerts on the specific stage.
"""

from __future__ import annotations

from meridian_common.errors import MeridianError, NotFoundError, ValidationError


class EvalkitError(MeridianError):
    """Base class for every evalkit error."""

    code = "evalkit.error"


class HarnessError(EvalkitError):
    """The benchmark harness could not run an item through the model or score it."""

    code = "evalkit.harness"


class MetricError(EvalkitError, ValidationError):
    """A metric was given inconsistent inputs (empty records, a bad ``k``, mismatched dimensions)."""

    code = "evalkit.metric"


class RunnerError(EvalkitError):
    """An eval runner could not drive the serving stack over its items."""

    code = "evalkit.runner"


class StoreError(EvalkitError):
    """The results store was given an inconsistent run or an unknown version."""

    code = "evalkit.store"


class VersionNotFoundError(StoreError, NotFoundError):
    """A requested results-store version does not exist."""

    code = "evalkit.store.version_not_found"


class LeaderboardError(EvalkitError):
    """The leaderboard was given inconsistent entries or an empty board."""

    code = "evalkit.leaderboard"


class BenchError(EvalkitError):
    """The throughput/cost benchmark was given an empty or malformed workload."""

    code = "evalkit.bench"
