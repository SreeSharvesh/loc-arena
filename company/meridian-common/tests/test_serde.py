from __future__ import annotations

import pytest

from meridian_common.config import Field, Schema
from meridian_common.errors import NotFoundError, ValidationError
from meridian_common.serde import Envelope, SchemaRegistry, canonical_json, fingerprint


def test_canonical_is_order_independent() -> None:
    a = {"b": 1, "a": [3, 2], "c": {"y": 1, "x": 2}}
    b = {"c": {"x": 2, "y": 1}, "a": [3, 2], "b": 1}
    assert canonical_json(a) == canonical_json(b)
    assert fingerprint(a) == fingerprint(b)


def test_canonical_rejects_nan() -> None:
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


def test_registry_versions_and_latest() -> None:
    reg = SchemaRegistry()
    reg.register("job", 1, Schema({"name": Field(str)}))
    reg.register("job", 2, Schema({"name": Field(str), "priority": Field(int, required=False, default=0)}))
    assert reg.latest_version("job") == 2
    with pytest.raises(ValidationError):
        reg.register("job", 2, Schema({}))  # duplicate version
    with pytest.raises(NotFoundError):
        reg.get("job", 9)


def test_registry_validate_fills_defaults() -> None:
    reg = SchemaRegistry()
    reg.register("job", 2, Schema({"name": Field(str), "priority": Field(int, required=False, default=0)}))
    assert reg.validate("job", 2, {"name": "x"}) == {"name": "x", "priority": 0}


def test_envelope_wrap_roundtrip_and_content_id() -> None:
    env = Envelope.wrap("job.submitted", 1, {"name": "eval", "priority": 5}, correlation_id="cid-1")
    restored = Envelope.from_dict(env.to_dict())
    assert restored == env
    assert restored.content_id == env.content_id


def test_envelope_detects_tampering() -> None:
    env = Envelope.wrap("job.submitted", 1, {"name": "eval"})
    tampered = {**env.to_dict(), "payload": {"name": "HACKED"}}
    with pytest.raises(ValidationError):
        Envelope.from_dict(tampered)


def test_envelope_validates_against_registry() -> None:
    reg = SchemaRegistry()
    reg.register("job.submitted", 1, Schema({"name": Field(str), "priority": Field(int)}))
    env = Envelope.wrap("job.submitted", 1, {"name": "eval", "priority": 5})
    assert env.validate_against(reg) == {"name": "eval", "priority": 5}
