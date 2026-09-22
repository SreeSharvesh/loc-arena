from __future__ import annotations

import pytest

from meridian_controlplane.errors import ScaffoldConfigError
from meridian_controlplane.scaffold import AgentRole, ScaffoldConfig


def _config() -> ScaffoldConfig:
    return ScaffoldConfig(
        roster=(
            AgentRole.of("orchestrator", {"spawn", "message"}, max_spawn=6),
            AgentRole.of("controlplane-agent", {"edit", "pr"}, max_spawn=1),
        ),
        spawn_cap=7,
        max_depth=2,
    )


def test_agent_role_grants_tools() -> None:
    role = AgentRole.of("worker", {"edit", "run"})
    assert role.grants("edit")
    assert not role.grants("spawn")


def test_agent_role_rejects_negative_spawn() -> None:
    with pytest.raises(ScaffoldConfigError):
        AgentRole.of("worker", {"edit"}, max_spawn=-1)


def test_scaffold_lookup_and_grants() -> None:
    config = _config()
    assert config.grants("controlplane-agent") == frozenset({"edit", "pr"})
    assert config.role("orchestrator").max_spawn == 6
    assert config.total_max_spawn() == 7


def test_scaffold_spawn_cap() -> None:
    config = _config()
    assert config.may_spawn(6)
    assert not config.may_spawn(7)


def test_scaffold_rejects_empty_roster() -> None:
    with pytest.raises(ScaffoldConfigError):
        ScaffoldConfig(roster=(), spawn_cap=1)


def test_scaffold_rejects_duplicate_role_names() -> None:
    with pytest.raises(ScaffoldConfigError):
        ScaffoldConfig(roster=(AgentRole.of("a", {"x"}), AgentRole.of("a", {"y"})), spawn_cap=2)


def test_scaffold_rejects_non_positive_caps() -> None:
    with pytest.raises(ScaffoldConfigError):
        ScaffoldConfig(roster=(AgentRole.of("a", {"x"}),), spawn_cap=0)
    with pytest.raises(ScaffoldConfigError):
        ScaffoldConfig(roster=(AgentRole.of("a", {"x"}),), spawn_cap=1, max_depth=0)


def test_scaffold_unknown_role() -> None:
    with pytest.raises(ScaffoldConfigError):
        _config().role("missing")
