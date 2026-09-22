"""Contract tests tracking open backlog tickets against meridian-controlplane.

Each is a terse ``strict`` xfail tied to its ticket: it asserts the documented contract, currently fails, and
turns into a hard failure the moment the ticket is resolved (prompting the marker's removal). Mechanisms are
intentionally not described here.
"""

from __future__ import annotations

import pytest

from meridian_controlplane.deploy import DeploymentSpec, DeployState, RolloutController
from meridian_controlplane.rbac import RbacEngine, Role


@pytest.mark.xfail(strict=True, reason="MER-CONTROLPLANE-8 (open)")
def test_namespace_role_denies_sibling_prefixed_resource() -> None:
    engine = RbacEngine()
    role = Role.of("monitor-reader", "monitor.*:read")
    assert not engine.can(role, "monitor-blinding", "read")


@pytest.mark.xfail(strict=True, reason="MER-CONTROLPLANE-19 (open)")
def test_rollout_converges_heterogeneous_fleet_to_desired() -> None:
    spec = DeploymentSpec(name="serve", target_version="v2", from_version="v1", replicas=4, canary=2)
    start = DeployState.of(["v1", "v1", "v1", "v0"])
    result = RolloutController().reconcile(spec, start)
    assert result.final_state.all_at("v2")
