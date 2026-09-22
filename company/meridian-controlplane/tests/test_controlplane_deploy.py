from __future__ import annotations

import pytest

from meridian_controlplane.deploy import (
    DeploymentSpec,
    DeployState,
    RolloutController,
)
from meridian_controlplane.errors import RolloutError


def test_spec_validates_replicas_and_canary() -> None:
    with pytest.raises(RolloutError):
        DeploymentSpec(name="d", target_version="v2", from_version="v1", replicas=0)
    with pytest.raises(RolloutError):
        DeploymentSpec(name="d", target_version="v2", from_version="v1", replicas=3, canary=4)


def test_rollout_converges_uniform_fleet() -> None:
    spec = DeploymentSpec(name="serve", target_version="v2", from_version="v1", replicas=4, canary=2)
    result = RolloutController().rollout(spec)
    assert result.converged
    assert result.final_state.all_at("v2")
    assert result.final_state == spec.desired_state()


def test_rollout_trajectory_shows_canary_then_full() -> None:
    spec = DeploymentSpec(name="serve", target_version="v2", from_version="v1", replicas=4, canary=2)
    result = RolloutController().rollout(spec)
    assert result.steps == 2
    initial, after_canary, after_full = result.trajectory
    assert initial.count("v1") == 4
    assert after_canary.count("v2") == 2
    assert after_full.count("v2") == 4


def test_reconcile_from_explicit_state() -> None:
    spec = DeploymentSpec(name="serve", target_version="v2", from_version="v1", replicas=3, canary=1)
    start = DeployState.uniform("v1", 3)
    result = RolloutController().reconcile(spec, start)
    assert result.converged
    assert result.final_state.histogram() == {"v2": 3}


def test_deploy_state_helpers() -> None:
    state = DeployState.of(["v1", "v2", "v2"])
    assert state.replicas == 3
    assert state.count("v2") == 2
    assert not state.all_at("v2")
    assert state.histogram() == {"v1": 1, "v2": 2}


def test_canary_zero_defers_all_to_full_phase() -> None:
    spec = DeploymentSpec(name="serve", target_version="v2", from_version="v1", replicas=3, canary=0)
    result = RolloutController().rollout(spec)
    assert result.trajectory[1].count("v2") == 0
    assert result.converged
