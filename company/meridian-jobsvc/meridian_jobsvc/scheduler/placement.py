"""Capacity-based job placement onto cluster nodes.

The scheduler holds a set of :class:`~meridian_jobsvc.types.Node` objects and tracks how much cpu and memory
each has free. :meth:`assign` places a job on the first node (in id order) that can hold its resource ask and
reserves that capacity until the job is released. A cordoned node accepts no new placements. A job whose ask
exceeds every node's *total* capacity can never be placed and raises :class:`SchedulingError`; a job that
merely does not fit *right now* returns ``None`` and waits.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian_common.jobclient.models import JobSpec
from meridian_jobsvc.errors import SchedulingError
from meridian_jobsvc.types import Node


@dataclass
class _Reservation:
    node_id: str
    cpu: float
    memory_mb: int


class Scheduler:
    """Places jobs on nodes under cpu/memory capacity and tracks reservations for release."""

    def __init__(self, nodes: list[Node]) -> None:
        """Start with ``nodes`` all fully free."""
        self._nodes: dict[str, Node] = {}
        self._free_cpu: dict[str, float] = {}
        self._free_mem: dict[str, int] = {}
        self._reservations: dict[str, _Reservation] = {}
        for node in nodes:
            self.add_node(node)

    def add_node(self, node: Node) -> None:
        """Add ``node`` to the pool with its full capacity free (idempotent on node id)."""
        if node.node_id in self._nodes:
            return
        self._nodes[node.node_id] = node
        self._free_cpu[node.node_id] = node.cpu_capacity
        self._free_mem[node.node_id] = node.memory_mb

    def remove_node(self, node_id: str) -> None:
        """Remove an empty node from the pool; a node still holding reservations is left in place."""
        if any(res.node_id == node_id for res in self._reservations.values()):
            return
        self._nodes.pop(node_id, None)
        self._free_cpu.pop(node_id, None)
        self._free_mem.pop(node_id, None)

    def node(self, node_id: str) -> Node:
        """The node with id ``node_id`` (raises :class:`KeyError` if unknown)."""
        return self._nodes[node_id]

    @property
    def node_ids(self) -> tuple[str, ...]:
        """The pool's node ids in id order."""
        return tuple(sorted(self._nodes))

    @property
    def node_count(self) -> int:
        """The number of nodes in the pool."""
        return len(self._nodes)

    def _fits_anywhere(self, spec: JobSpec) -> bool:
        return any(
            spec.cpu <= node.cpu_capacity and spec.memory_mb <= node.memory_mb
            for node in self._nodes.values()
        )

    def assign(self, job_id: str, spec: JobSpec) -> str | None:
        """Reserve capacity for ``job_id`` on the first eligible node; return its id or ``None`` if full.

        Raises:
            SchedulingError: if the ask exceeds every node's total capacity (never schedulable).
        """
        if not self._fits_anywhere(spec):
            raise SchedulingError(
                f"job {spec.name!r} asks {spec.cpu} cpu / {spec.memory_mb} MB, larger than any node",
                path=job_id,
            )
        for node_id in sorted(self._nodes):
            if self._nodes[node_id].cordoned:
                continue
            if self._free_cpu[node_id] >= spec.cpu and self._free_mem[node_id] >= spec.memory_mb:
                self._free_cpu[node_id] -= spec.cpu
                self._free_mem[node_id] -= spec.memory_mb
                self._reservations[job_id] = _Reservation(node_id, spec.cpu, spec.memory_mb)
                return node_id
        return None

    def release(self, job_id: str) -> None:
        """Free the capacity ``job_id`` reserved (no-op if it holds no reservation)."""
        res = self._reservations.pop(job_id, None)
        if res is None:
            return
        if res.node_id in self._free_cpu:
            self._free_cpu[res.node_id] += res.cpu
            self._free_mem[res.node_id] += res.memory_mb

    def node_of(self, job_id: str) -> str | None:
        """The node a reserved job sits on, or ``None`` if it holds no reservation."""
        res = self._reservations.get(job_id)
        return res.node_id if res is not None else None

    def free_cpu(self, node_id: str) -> float:
        """The cpu still free on ``node_id``."""
        return self._free_cpu[node_id]

    def reserved_cpu(self) -> float:
        """The total cpu reserved across the pool (the current demand)."""
        return sum(res.cpu for res in self._reservations.values())

    def total_cpu(self) -> float:
        """The total cpu capacity across all nodes."""
        return sum(node.cpu_capacity for node in self._nodes.values())

    def utilization(self) -> float:
        """Reserved cpu as a fraction of total cpu capacity (0.0 when the pool is empty)."""
        total = self.total_cpu()
        return self.reserved_cpu() / total if total else 0.0
