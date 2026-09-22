"""Deployment value types: the desired spec, the deployed state, and a rollout result.

A :class:`DeploymentSpec` is the desired end state of a rollout: a target version across a fixed number of
replicas, with a canary size that is promoted first. A :class:`DeployState` is the concrete per-replica
version vector currently deployed. A :class:`RolloutResult` is the outcome of driving a state toward a spec:
the final state, the step-by-step trajectory, and whether the fleet converged on the target.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from meridian_controlplane.errors import RolloutError


@dataclass(frozen=True)
class DeploymentSpec:
    """The desired end state of a rollout.

    Attributes:
        name: the deployment's stable name.
        target_version: the version every replica should end on.
        from_version: the version a rollout promotes replicas away from.
        replicas: the constant fleet size.
        canary: how many replicas are promoted in the first (canary) phase before the rest.
    """

    name: str
    target_version: str
    from_version: str
    replicas: int
    canary: int = 1

    def __post_init__(self) -> None:
        """Validate the fleet size and canary size."""
        if self.replicas < 1:
            raise RolloutError(f"replicas must be >= 1, got {self.replicas}", path="replicas")
        if not 0 <= self.canary <= self.replicas:
            raise RolloutError(f"canary must be within [0, replicas], got {self.canary}", path="canary")
        if not self.target_version:
            raise RolloutError("target_version must be non-empty", path="target_version")

    def desired_state(self) -> DeployState:
        """The :class:`DeployState` in which every replica runs the target version."""
        return DeployState(versions=(self.target_version,) * self.replicas)


@dataclass(frozen=True)
class DeployState:
    """The per-replica version vector currently deployed (one entry per replica)."""

    versions: tuple[str, ...]

    @classmethod
    def uniform(cls, version: str, replicas: int) -> DeployState:
        """A state in which every one of ``replicas`` replicas runs ``version``."""
        return cls(versions=(version,) * replicas)

    @classmethod
    def of(cls, versions: Iterable[str]) -> DeployState:
        """A state from an explicit per-replica version sequence."""
        return cls(versions=tuple(versions))

    @property
    def replicas(self) -> int:
        """The fleet size."""
        return len(self.versions)

    def count(self, version: str) -> int:
        """How many replicas currently run ``version``."""
        return sum(1 for v in self.versions if v == version)

    def histogram(self) -> dict[str, int]:
        """A version -> replica-count histogram."""
        return dict(Counter(self.versions))

    def all_at(self, version: str) -> bool:
        """Whether every replica runs ``version``."""
        return all(v == version for v in self.versions)


@dataclass(frozen=True)
class RolloutResult:
    """The outcome of a rollout: the final state, the trajectory, and whether it converged.

    ``trajectory`` includes the initial state as ``trajectory[0]`` and the final state as ``trajectory[-1]``,
    so a caller can see the canary phase and the full phase as intermediate states.
    """

    spec: DeploymentSpec
    final_state: DeployState
    trajectory: tuple[DeployState, ...]
    converged: bool

    @property
    def steps(self) -> int:
        """The number of transitions taken (one fewer than the trajectory length)."""
        return len(self.trajectory) - 1
