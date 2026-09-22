from __future__ import annotations

from meridian_common.errors import CapacityError, MeridianError, NotFoundError, ValidationError
from meridian_jobsvc.errors import (
    DependencyError,
    DrainError,
    JobNotFound,
    QuotaExceeded,
    SchedulingError,
    SpecSyntaxError,
)


def test_errors_extend_common_hierarchy() -> None:
    assert issubclass(JobNotFound, NotFoundError)
    assert issubclass(DependencyError, ValidationError)
    assert issubclass(SchedulingError, CapacityError)
    assert issubclass(QuotaExceeded, CapacityError)
    assert issubclass(SpecSyntaxError, ValidationError)
    assert issubclass(DrainError, MeridianError)


def test_error_codes_are_stable_and_distinct() -> None:
    codes = {
        JobNotFound.code,
        DependencyError.code,
        SchedulingError.code,
        DrainError.code,
        QuotaExceeded.code,
        SpecSyntaxError.code,
    }
    assert len(codes) == 6
    assert all(code.startswith("jobsvc.") for code in codes)


def test_error_carries_context_and_serializes() -> None:
    err = JobNotFound("nope", path="job-9")
    assert err.context["path"] == "job-9"
    assert err.to_dict()["code"] == "jobsvc.job_not_found"
    assert isinstance(err, MeridianError)
