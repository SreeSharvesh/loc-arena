from __future__ import annotations

from pathlib import Path

import pytest

from meridian_common.config import Field, Schema, load_config
from meridian_common.config.flags import FeatureFlags, Flag
from meridian_common.errors import ConfigError, NotFoundError, ValidationError


def test_layers_merge_with_env_winning(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    base.write_text("server:\n  host: localhost\n  port: 8080\nfeature: false\n")
    over = tmp_path / "over.yaml"
    over.write_text("server:\n  port: 9090\n")
    cfg = load_config(
        defaults={"server": {"host": "0.0.0.0", "timeout": 5}, "feature": True},
        files=[base, over],
        environ={"MERIDIAN_SERVER__HOST": "prod.internal", "MERIDIAN_FEATURE": "true"},
    )
    assert cfg["server"] == {
        "host": "prod.internal",
        "port": 9090,
        "timeout": 5,
    }  # env > over > base > default
    assert cfg["feature"] is True


def test_env_coercion_matches_target_type() -> None:
    cfg = load_config(
        defaults={"n": 1, "ratio": 0.5, "on": False, "name": "x"},
        environ={
            "MERIDIAN_N": "42",
            "MERIDIAN_RATIO": "0.25",
            "MERIDIAN_ON": "yes",
            "MERIDIAN_NAME": "meridian",
        },
    )
    assert cfg == {"n": 42, "ratio": 0.25, "on": True, "name": "meridian"}


def test_env_bad_int_raises() -> None:
    with pytest.raises(ConfigError):
        load_config(defaults={"n": 1}, environ={"MERIDIAN_N": "not-a-number"})


def test_missing_file_raises() -> None:
    with pytest.raises(ConfigError):
        load_config(files=[Path("/no/such/file.yaml")])


def test_schema_validates_fills_defaults_and_rejects_extra() -> None:
    schema = Schema(
        {
            "host": Field(str),
            "port": Field(int, validator=lambda v: 0 < v < 65536),
            "debug": Field(bool, required=False, default=False),
        }
    )
    out = load_config(defaults={"host": "h", "port": 80}, schema=schema)
    assert out == {"host": "h", "port": 80, "debug": False}
    with pytest.raises(ValidationError):
        load_config(defaults={"host": "h", "port": 80, "extra": 1}, schema=schema)
    with pytest.raises(ValidationError):
        load_config(defaults={"host": "h", "port": 999999}, schema=schema)  # validator fails


def test_schema_rejects_bool_for_int_and_missing_required() -> None:
    schema = Schema({"port": Field(int)})
    with pytest.raises(ValidationError):
        schema.validate({"port": True})
    with pytest.raises(ValidationError):
        schema.validate({})


def test_feature_flags_deterministic_and_overrides() -> None:
    flags = FeatureFlags.from_config(
        {
            "new_ui": {"rollout_percent": 50},
            "beta": {"default": True},
            "kill": {"default": True, "disabled_for": ["tenant-9"]},
        }
    )
    # deterministic in the entity id
    a = flags.is_enabled("new_ui", "tenant-1")
    assert flags.is_enabled("new_ui", "tenant-1") is a
    assert flags.is_enabled("beta") is True
    assert flags.is_enabled("kill", "tenant-9") is False  # override beats default
    with pytest.raises(NotFoundError):
        flags.is_enabled("missing")


def test_flag_full_and_zero_rollout() -> None:
    assert Flag("f", rollout_percent=100).is_enabled("anyone") is True
    assert Flag("f", default=False, rollout_percent=0).is_enabled("anyone") is False
