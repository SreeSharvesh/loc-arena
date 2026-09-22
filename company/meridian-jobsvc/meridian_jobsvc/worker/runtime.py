"""A worker runtime that hosts long-lived jobs step by step.

A job is *started* on a node with a fixed step count, then *stepped* one unit at a time; each step advances
progress, refreshes a heartbeat, and persists a :class:`~meridian_jobsvc.types.Checkpoint`. A job that reaches
its step count is *finished*. A running job can be *released* off its node (returning its checkpoint) or
*resumed* on another node from a checkpoint, which is how the lifecycle moves work between nodes without
losing it. Time is supplied by an injected clock (default: a monotonic per-step counter) so heartbeats and
stepping are deterministic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from meridian_jobsvc.errors import JobNotFound
from meridian_jobsvc.types import Checkpoint


@dataclass
class _RunningJob:
    job_id: str
    node_id: str
    total_steps: int
    step: int
    last_heartbeat: int

    @property
    def progress(self) -> float:
        return self.step / self.total_steps if self.total_steps else 1.0

    @property
    def finished(self) -> bool:
        return self.step >= self.total_steps

    def checkpoint(self) -> Checkpoint:
        return Checkpoint(step=self.step, progress=self.progress, total_steps=self.total_steps)


class WorkerRuntime:
    """Hosts long-lived jobs: start, step, heartbeat, checkpoint, release, and resume."""

    def __init__(self, *, clock: Callable[[], int] | None = None) -> None:
        """Use ``clock`` for heartbeat timestamps, or an internal monotonic per-step counter by default."""
        self._clock = clock
        self._tick = 0
        self._running: dict[str, _RunningJob] = {}
        self._finished: dict[str, Checkpoint] = {}
        self._node_slot: dict[str, str] = {}

    def _now(self) -> int:
        if self._clock is not None:
            return self._clock()
        self._tick += 1
        return self._tick

    def start(self, job_id: str, node_id: str, total_steps: int) -> None:
        """Start ``job_id`` on ``node_id`` from step zero with ``total_steps`` steps of work."""
        self.resume(job_id, node_id, total_steps, Checkpoint.start(max(total_steps, 1)))

    def resume(self, job_id: str, node_id: str, total_steps: int, checkpoint: Checkpoint) -> None:
        """Place ``job_id`` on ``node_id`` continuing from ``checkpoint`` (step and progress preserved)."""
        steps = max(total_steps, 1)
        self._finished.pop(job_id, None)
        self._running[job_id] = _RunningJob(
            job_id=job_id,
            node_id=node_id,
            total_steps=steps,
            step=min(checkpoint.step, steps),
            last_heartbeat=self._now(),
        )
        self._node_slot[node_id] = job_id

    def step(self, job_id: str) -> None:
        """Advance ``job_id`` by one unit of work, refreshing its heartbeat and checkpoint."""
        if job_id in self._finished:
            return
        job = self._require(job_id)
        if job.finished:
            return
        job.step += 1
        job.last_heartbeat = self._now()
        if job.finished:
            self._finished[job_id] = job.checkpoint()
            self._running.pop(job_id, None)

    def _require(self, job_id: str) -> _RunningJob:
        job = self._running.get(job_id)
        if job is None:
            raise JobNotFound(f"job {job_id!r} is not running", path=job_id)
        return job

    def is_running(self, job_id: str) -> bool:
        """Whether ``job_id`` is currently placed and unfinished."""
        return job_id in self._running

    def is_finished(self, job_id: str) -> bool:
        """Whether ``job_id`` has completed all its steps."""
        return job_id in self._finished

    def progress(self, job_id: str) -> float:
        """The fraction of ``job_id``'s work completed (1.0 once finished)."""
        if job_id in self._finished:
            return 1.0
        return self._require(job_id).progress

    def heartbeat(self, job_id: str) -> int:
        """The clock value at ``job_id``'s most recent step (or start)."""
        return self._require(job_id).last_heartbeat

    def checkpoint(self, job_id: str) -> Checkpoint:
        """The saved checkpoint for ``job_id`` (its own step and progress)."""
        if job_id in self._finished:
            return self._finished[job_id]
        return self._require(job_id).checkpoint()

    def node_resume_checkpoint(self, node_id: str) -> Checkpoint:
        """The resume checkpoint held for ``node_id``'s current execution slot."""
        slot = self._node_slot.get(node_id)
        if slot is not None and slot in self._running:
            return self._running[slot].checkpoint()
        return Checkpoint.start(1)

    def running_on(self, node_id: str) -> list[str]:
        """The ids of jobs currently running on ``node_id`` (in id order)."""
        return sorted(job_id for job_id, job in self._running.items() if job.node_id == node_id)

    def node_of(self, job_id: str) -> str | None:
        """The node ``job_id`` runs on, or ``None`` if it is not running."""
        job = self._running.get(job_id)
        return job.node_id if job is not None else None

    def release(self, job_id: str) -> Checkpoint:
        """Take ``job_id`` off its node and return the checkpoint it should resume from."""
        job = self._require(job_id)
        checkpoint = job.checkpoint()
        self._running.pop(job_id, None)
        if self._node_slot.get(job.node_id) == job_id:
            self._node_slot.pop(job.node_id, None)
        return checkpoint
