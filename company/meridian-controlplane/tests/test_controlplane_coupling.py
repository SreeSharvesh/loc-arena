"""Cross-repo coupling: the control plane's identity and RBAC layers ride on meridian-common's scopes.

The control plane does not define its own scope grammar; roles and service accounts are expressed in the
platform ``resource:action`` grammar that ``meridian_common.authclient`` owns, and control-plane errors are
catchable as ``meridian_common.errors.MeridianError``. These tests fail if either contract drifts.
"""

from __future__ import annotations

from meridian_common.authclient import ScopeSet, parse_scopes
from meridian_common.errors import MeridianError
from meridian_controlplane.errors import PolicyError
from meridian_controlplane.identity import ServiceAccount
from meridian_controlplane.rbac import RbacEngine, Role


def test_role_scope_set_is_a_common_scope_set() -> None:
    role = Role.of("editor", "policy:read policy:write")
    assert isinstance(role.scope_set(), ScopeSet)
    assert role.scope_set().allows("policy", "read")


def test_engine_agrees_with_common_scopeset_on_plain_scopes() -> None:
    engine = RbacEngine()
    raw = "policy:read jobsvc:submit"
    role = Role.of("svc", raw)
    reference = parse_scopes(raw)
    for resource, action in [
        ("policy", "read"),
        ("policy", "write"),
        ("jobsvc", "submit"),
        ("secrets", "read"),
    ]:
        assert engine.can(role, resource, action) == reference.allows(resource, action)


def test_service_account_scopes_share_the_platform_grammar() -> None:
    account = ServiceAccount(name="deployer", scopes="repo:read jobsvc:*")
    scopes = account.scope_set()
    assert scopes.allows("jobsvc", "cancel")
    assert not scopes.allows("secrets", "read")


def test_control_plane_errors_are_catchable_as_common_errors() -> None:
    try:
        raise PolicyError("boom", path="component")
    except MeridianError as err:
        assert err.code == "controlplane.policy"
