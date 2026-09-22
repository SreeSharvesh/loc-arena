"""Scaffold configuration: the agent roster, spawn caps, and tool grants.

An :class:`AgentRole` names one role in the agent roster, the tools it is granted, and how many sub-agents it
may spawn. A :class:`ScaffoldConfig` collects the roster and the global spawn cap and depth, validates it
(unique role names, positive caps), and answers the questions the orchestrator asks: what a role is granted,
and whether another spawn is admissible.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from meridian_controlplane.errors import ScaffoldConfigError


@dataclass(frozen=True)
class AgentRole:
    """One role in the agent roster: a name, its granted tools, and its per-role spawn cap."""

    name: str
    tools: frozenset[str]
    max_spawn: int = 0

    @classmethod
    def of(cls, name: str, tools: Iterable[str], max_spawn: int = 0) -> AgentRole:
        """Build a role granting ``tools`` and permitted to spawn up to ``max_spawn`` sub-agents."""
        if not name.strip():
            raise ScaffoldConfigError("agent role name must be non-empty", path="name")
        if max_spawn < 0:
            raise ScaffoldConfigError(f"max_spawn must be >= 0, got {max_spawn}", path="max_spawn")
        return cls(name=name, tools=frozenset(tools), max_spawn=max_spawn)

    def grants(self, tool: str) -> bool:
        """Whether this role is granted ``tool``."""
        return tool in self.tools


@dataclass(frozen=True)
class ScaffoldConfig:
    """A validated scaffold configuration: the roster, the global spawn cap, and the max spawn depth."""

    roster: tuple[AgentRole, ...]
    spawn_cap: int
    max_depth: int = 1

    def __post_init__(self) -> None:
        """Reject an empty roster, duplicate role names, or non-positive caps."""
        if not self.roster:
            raise ScaffoldConfigError("scaffold roster must be non-empty", path="roster")
        names = [role.name for role in self.roster]
        if len(set(names)) != len(names):
            raise ScaffoldConfigError("agent role names must be unique", path="roster")
        if self.spawn_cap < 1:
            raise ScaffoldConfigError(f"spawn_cap must be >= 1, got {self.spawn_cap}", path="spawn_cap")
        if self.max_depth < 1:
            raise ScaffoldConfigError(f"max_depth must be >= 1, got {self.max_depth}", path="max_depth")

    def role(self, name: str) -> AgentRole:
        """The role named ``name``.

        Raises:
            ScaffoldConfigError: if no role has that name.
        """
        for role in self.roster:
            if role.name == name:
                return role
        raise ScaffoldConfigError(f"unknown agent role {name!r}", path="roster")

    def grants(self, role_name: str) -> frozenset[str]:
        """The tools granted to ``role_name``."""
        return self.role(role_name).tools

    def may_spawn(self, current_agents: int) -> bool:
        """Whether another sub-agent may be spawned given ``current_agents`` already live."""
        return current_agents < self.spawn_cap

    def total_max_spawn(self) -> int:
        """The sum of per-role spawn caps across the roster."""
        return sum(role.max_spawn for role in self.roster)
