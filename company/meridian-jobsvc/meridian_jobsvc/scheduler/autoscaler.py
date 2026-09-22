"""A cluster autoscaler simulation.

Given the current node count and the current cpu demand, the autoscaler decides a target node count that keeps
utilization inside a band ``[target_low, target_high]``: it grows the pool when utilization runs above the
band and shrinks it when utilization runs below, within ``[min_nodes, max_nodes]``. :meth:`simulate` runs the
control loop under a constant demand and returns the node-count trajectory, so a caller can see where the pool
comes to rest. Everything is deterministic (no wall clock, no randomness), so a trajectory is reproducible
from its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScalePolicy:
    """The autoscaler's target band and bounds.

    Attributes:
        per_node_cpu: cpu capacity contributed by one node.
        target_low: utilization below which the pool should shrink.
        target_high: utilization above which the pool should grow.
        min_nodes: the smallest pool the autoscaler will scale down to.
        max_nodes: the largest pool the autoscaler will scale up to.
        step: how many nodes are added or removed in one scaling decision.
    """

    per_node_cpu: float = 8.0
    target_low: float = 0.55
    target_high: float = 0.85
    min_nodes: int = 1
    max_nodes: int = 64
    step: int = 1


class Autoscaler:
    """Drives node count toward a utilization band from the current demand."""

    def __init__(self, policy: ScalePolicy | None = None) -> None:
        """Use ``policy`` (or the default band) for every decision."""
        self._policy = policy if policy is not None else ScalePolicy()

    @property
    def policy(self) -> ScalePolicy:
        """The band and bounds this autoscaler applies."""
        return self._policy

    def utilization(self, node_count: int, demand_cpu: float) -> float:
        """Demand as a fraction of the pool's cpu capacity at ``node_count`` nodes."""
        capacity = node_count * self._policy.per_node_cpu
        return demand_cpu / capacity if capacity > 0 else float("inf")

    def desired(self, node_count: int, demand_cpu: float) -> int:
        """The target node count for the next step given the current count and demand."""
        p = self._policy
        util = self.utilization(node_count, demand_cpu)
        if util > p.target_high:
            return min(node_count + p.step, p.max_nodes)
        if util < p.target_low:
            return max(node_count - p.step, p.min_nodes)
        return node_count

    def simulate(self, demand_cpu: float, *, initial_nodes: int, steps: int) -> list[int]:
        """Run the control loop for ``steps`` iterations under a constant demand.

        Returns the node-count trajectory including the initial count, so ``result[0]`` is ``initial_nodes``
        and ``result[-1]`` is the count after the last decision.
        """
        counts = [initial_nodes]
        node_count = initial_nodes
        for _ in range(steps):
            node_count = self.desired(node_count, demand_cpu)
            counts.append(node_count)
        return counts

    def settles(self, demand_cpu: float, *, initial_nodes: int, steps: int, window: int = 4) -> bool:
        """Whether the pool comes to rest under a constant demand: the last ``window`` counts are equal."""
        trajectory = self.simulate(demand_cpu, initial_nodes=initial_nodes, steps=steps)
        tail = trajectory[-window:]
        return len(set(tail)) == 1
