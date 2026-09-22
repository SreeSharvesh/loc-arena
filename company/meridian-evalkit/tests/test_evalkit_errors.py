"""Tests for the evalkit error hierarchy."""

from __future__ import annotations

import pytest

from meridian_common.errors import MeridianError, NotFoundError, ValidationError
from meridian_evalkit.errors import (
    BenchError,
    EvalkitError,
    HarnessError,
    LeaderboardError,
    MetricError,
    RunnerError,
    StoreError,
    VersionNotFoundError,
)


def test_evalkit_error_is_meridian_error() -> None:
    assert issubclass(EvalkitError, MeridianError)


@pytest.mark.parametrize(
    "cls",
    [HarnessError, MetricError, RunnerError, StoreError, LeaderboardError, BenchError],
)
def test_evalkit_errors_extend_base(cls: type[EvalkitError]) -> None:
    assert issubclass(cls, EvalkitError)


def test_metric_error_is_validation_error() -> None:
    assert issubclass(MetricError, ValidationError)


def test_version_not_found_is_store_and_not_found() -> None:
    assert issubclass(VersionNotFoundError, StoreError)
    assert issubclass(VersionNotFoundError, NotFoundError)


def test_error_carries_code_and_context() -> None:
    err = HarnessError("boom", item_id="q1")
    assert err.code == "evalkit.harness"
    assert err.context == {"item_id": "q1"}
    assert err.to_dict()["code"] == "evalkit.harness"


def test_code_override() -> None:
    err = MetricError("bad", code="evalkit.metric.custom", k=0)
    assert err.code == "evalkit.metric.custom"
    assert str(err) == "evalkit.metric.custom: bad"
