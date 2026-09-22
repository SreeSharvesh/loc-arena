from __future__ import annotations

import pytest

from meridian_controlplane.errors import RbacError
from meridian_controlplane.rbac import RbacEngine, Role


def test_role_parses_scopes_from_grammar() -> None:
    role = Role.of("editor", "policy:read policy:write")
    assert role.granted() == ("policy:read", "policy:write")


def test_exact_scope_grants_only_its_resource_action() -> None:
    engine = RbacEngine()
    role = Role.of("reader", "policy:read")
    assert engine.can(role, "policy", "read")
    assert not engine.can(role, "policy", "write")
    assert not engine.can(role, "secrets", "read")


def test_action_wildcard_grants_all_actions_on_resource() -> None:
    engine = RbacEngine()
    role = Role.of("owner", "policy:*")
    assert engine.can(role, "policy", "read")
    assert engine.can(role, "policy", "write")
    assert engine.can(role, "policy", "delete")
    assert not engine.can(role, "secrets", "read")


def test_global_wildcard_grants_everything() -> None:
    engine = RbacEngine()
    role = Role.of("admin", "*")
    assert engine.can(role, "anything", "whatever")


def test_namespace_scope_grants_children() -> None:
    engine = RbacEngine()
    role = Role.of("monitor-reader", "monitor.*:read")
    assert engine.can(role, "monitor.policy", "read")
    assert engine.can(role, "monitor.logging", "read")
    assert not engine.can(role, "monitor.policy", "write")
    assert not engine.can(role, "secrets.rotate", "read")


def test_can_rejects_empty_resource_or_action() -> None:
    engine = RbacEngine()
    role = Role.of("reader", "policy:read")
    with pytest.raises(RbacError):
        engine.can(role, "", "read")
    with pytest.raises(RbacError):
        engine.can(role, "policy", "")


def test_require_raises_on_denied_and_passes_on_allowed() -> None:
    engine = RbacEngine()
    role = Role.of("reader", "policy:read")
    engine.require(role, "policy", "read")
    with pytest.raises(RbacError):
        engine.require(role, "policy", "write")
