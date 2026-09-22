"""Internal value types for the cluster service.

The wire contract (:class:`~meridian_common.jobclient.models.JobSpec`,
:class:`~meridian_common.jobclient.models.JobStatus`, :class:`~meridian_common.jobclient.models.JobState`)
is owned by meridian-common and re-used verbatim; this module adds the cluster-internal shapes those DTOs do
not cover: a :class:`Node` (a unit of capacity), a :class:`Checkpoint` (a job's saved progress), and a
:class:`DrainedJob` (a job evicted off a node together with the checkpoint it should resume from).
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian_common.jobclient.models import JobSpec, JobState, JobStatus

__all__ = ["Checkpoint", "DrainedJob", "JobSpec", "JobState", "JobStatus", "Node"]


@dataclass
class Node:
    """A cluster node: an id, its cpu and memory capacity, and whether it is cordoned.

    A cordoned node keeps running the jobs already placed on it but accepts no new placements; capacity is
    tracked by the scheduler, not here, so a node is a stable description of what the machine can hold.
    """

    node_id: str
    cpu_capacity: float = 4.0
    memory_mb: int = 4096
    cordoned: bool = False


@dataclass(frozen=True)
class Checkpoint:
    """A job's saved progress: the completed step count, its fraction of the whole, and the step total.

    A checkpoint is what a job resumes from after it is moved between nodes: resuming from ``Checkpoint(step,
    ...)`` means the job continues at step ``step`` rather than restarting at zero.
    """

    step: int
    progress: float
    total_steps: int

    @classmethod
    def start(cls, total_steps: int) -> Checkpoint:
        """The checkpoint of a job that has not run any steps yet."""
        return cls(step=0, progress=0.0, total_steps=total_steps)


@dataclass(frozen=True)
class DrainedJob:
    """A job evicted off a node during a drain, paired with the checkpoint it should resume from."""

    job_id: str
    checkpoint: Checkpoint
