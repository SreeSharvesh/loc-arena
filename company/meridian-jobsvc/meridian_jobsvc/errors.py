"""Jobsvc-specific errors, extending the shared Meridian hierarchy.

The cluster service raises these so callers can tell a missing-job fault from a dependency fault from a
scheduling, drain, quota, or spec-syntax fault. They all derive from
:class:`meridian_common.errors.MeridianError`, so a generic handler still catches them, while the stable
``code`` keys alerts on the specific stage.
"""

from __future__ import annotations

from meridian_common.errors import CapacityError, MeridianError, NotFoundError, ValidationError


class JobNotFound(NotFoundError):  # noqa: N818 - reads as a state, not an Error type
    """A referenced job id is not known to the service."""

    code = "jobsvc.job_not_found"


class DependencyError(ValidationError):
    """A job's ``depends_on`` set is unsatisfiable: an unknown dependency or a dependency cycle."""

    code = "jobsvc.dependency"


class SchedulingError(CapacityError):
    """A job cannot be placed because its resource ask exceeds every node's total capacity."""

    code = "jobsvc.scheduling"


class DrainError(MeridianError):
    """A node lifecycle operation (cordon, drain, rebalance) was asked to act on an unknown node."""

    code = "jobsvc.drain"


class QuotaExceeded(CapacityError):  # noqa: N818 - reads as a state, not an Error type
    """A charge would push an identity's spend past its configured quota."""

    code = "jobsvc.quota"


class SpecSyntaxError(ValidationError):
    """A job-spec DSL string was malformed and could not be parsed into a :class:`JobSpec`."""

    code = "jobsvc.spec_syntax"
