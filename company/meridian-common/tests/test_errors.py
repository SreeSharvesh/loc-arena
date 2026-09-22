from __future__ import annotations

import pytest

from meridian_common.errors import (
    CircuitOpenError,
    ConfigError,
    DeadlineExceeded,
    MeridianError,
    TransportError,
)


def test_error_carries_code_and_context() -> None:
    err = ConfigError("bad", missing="port")
    assert err.code == "config.error"
    assert err.context == {"missing": "port"}
    assert str(err) == "config.error: bad"
    assert err.to_dict() == {"code": "config.error", "message": "bad", "context": {"missing": "port"}}


def test_code_override() -> None:
    err = MeridianError("x", code="custom.code", k=1)
    assert err.code == "custom.code"


def test_hierarchy_is_catchable_by_category() -> None:
    assert isinstance(CircuitOpenError("open"), TransportError)
    assert isinstance(DeadlineExceeded("late"), MeridianError)
    with pytest.raises(MeridianError):
        raise CircuitOpenError("open")


def test_transport_and_config_are_distinct_branches() -> None:
    assert not isinstance(ConfigError("x"), TransportError)
    assert not isinstance(TransportError("x"), ConfigError)
