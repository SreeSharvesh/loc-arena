"""Node lifecycle operations: cordon, drain, and rebalance.

Cordoning marks a node so the scheduler places nothing new on it. Draining evicts every job off a node and
hands each back as a :class:`~meridian_jobsvc.types.DrainedJob` carrying the checkpoint it should resume from,
so the caller can place it elsewhere and it continues rather than restarting. Rebalancing evens the job count
across nodes by relocating jobs off the busiest nodes. This module coordinates the scheduler (capacity) and
the worker runtime (running jobs and their checkpoints); it does not itself re-place work — the service does,
using the returned checkpoints.
"""

from __future__ import annotations

from math import ceil

from meridian_jobsvc.errors import DrainError
from meridian_jobsvc.scheduler.placement import Scheduler
from meridian_jobsvc.types import DrainedJob
from meridian_jobsvc.worker.runtime import WorkerRuntime


class NodeLifecycle:
    """Cordon, drain, and rebalance nodes across the scheduler and the worker runtime."""

    def __init__(self, scheduler: Scheduler, worker: WorkerRuntime) -> None:
        """Operate over ``scheduler``'s node pool and the jobs hosted by ``worker``."""
        self._scheduler = scheduler
        self._worker = worker

    def _require_node(self, node_id: str) -> None:
        if node_id not in self._scheduler.node_ids:
            raise DrainError(f"unknown node {node_id!r}", path=node_id)

    def cordon(self, node_id: str) -> None:
        """Mark ``node_id`` so the scheduler places no new jobs on it (running jobs stay)."""
        self._require_node(node_id)
        self._scheduler.node(node_id).cordoned = True

    def uncordon(self, node_id: str) -> None:
        """Clear the cordon on ``node_id`` so it can accept placements again."""
        self._require_node(node_id)
        self._scheduler.node(node_id).cordoned = False

    def is_cordoned(self, node_id: str) -> bool:
        """Whether ``node_id`` is cordoned."""
        self._require_node(node_id)
        return self._scheduler.node(node_id).cordoned

    def drain(self, node_id: str) -> list[DrainedJob]:
        """Cordon ``node_id`` and evict every job on it, each with the checkpoint to resume from."""
        self._require_node(node_id)
        self.cordon(node_id)
        resume_point = self._worker.node_resume_checkpoint(node_id)
        drained: list[DrainedJob] = []
        for job_id in self._worker.running_on(node_id):
            self._worker.release(job_id)
            self._scheduler.release(job_id)
            drained.append(DrainedJob(job_id=job_id, checkpoint=resume_point))
        return drained

    def relocate(self, job_id: str) -> DrainedJob:
        """Take a single running ``job_id`` off its node, returning its own checkpoint to resume from."""
        checkpoint = self._worker.release(job_id)
        self._scheduler.release(job_id)
        return DrainedJob(job_id=job_id, checkpoint=checkpoint)

    def rebalance(self) -> list[DrainedJob]:
        """Relocate jobs off the busiest nodes so the per-node job count is as even as possible.

        Returns the jobs taken off their nodes (each with its own checkpoint) for the caller to re-place on
        the now-lighter nodes; an already-even cluster returns an empty list.
        """
        node_ids = self._scheduler.node_ids
        if not node_ids:
            return []
        counts = {nid: self._worker.running_on(nid) for nid in node_ids}
        total = sum(len(jobs) for jobs in counts.values())
        target = ceil(total / len(node_ids))
        moved: list[DrainedJob] = []
        for nid in node_ids:
            jobs = counts[nid]
            for job_id in jobs[target:]:
                moved.append(self.relocate(job_id))
        return moved
