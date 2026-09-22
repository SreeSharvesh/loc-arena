from __future__ import annotations

from meridian_common.errors import ConfigError, MeridianError, ValidationError
from meridian_controlplane.errors import (
    CiConfigError,
    IdentityConfigError,
    PolicyError,
    RbacError,
    RolloutError,
    ScaffoldConfigError,
)


def test_all_controlplane_errors_are_meridian_errors() -> None:
    for cls in (
        PolicyError,
        RolloutError,
        RbacError,
        IdentityConfigError,
        CiConfigError,
        ScaffoldConfigError,
    ):
        assert issubclass(cls, MeridianError)


def test_error_category_bases() -> None:
    assert issubclass(PolicyError, ValidationError)
    assert issubclass(RbacError, ValidationError)
    assert issubclass(IdentityConfigError, ConfigError)
    assert issubclass(CiConfigError, ConfigError)
    assert issubclass(ScaffoldConfigError, ConfigError)
    assert issubclass(RolloutError, MeridianError)


def test_error_codes_are_namespaced_and_distinct() -> None:
    codes = {
        PolicyError.code,
        RolloutError.code,
        RbacError.code,
        IdentityConfigError.code,
        CiConfigError.code,
        ScaffoldConfigError.code,
    }
    assert len(codes) == 6
    assert all(code.startswith("controlplane.") for code in codes)


def test_error_carries_context_and_serializes() -> None:
    err = PolicyError("bad", path="component", component="monitor")
    assert err.context["component"] == "monitor"
    assert err.to_dict()["code"] == "controlplane.policy"
    assert isinstance(err, MeridianError)
