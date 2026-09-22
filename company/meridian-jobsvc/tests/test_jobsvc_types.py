from __future__ import annotations

from meridian_jobsvc.types import Checkpoint, DrainedJob, JobSpec, JobState, Node


def test_node_defaults_and_cordon_flag() -> None:
    node = Node("n0")
    assert node.cpu_capacity == 4.0
    assert node.memory_mb == 4096
    assert node.cordoned is False
    node.cordoned = True
    assert node.cordoned is True


def test_checkpoint_start_is_zero() -> None:
    cp = Checkpoint.start(8)
    assert cp.step == 0
    assert cp.progress == 0.0
    assert cp.total_steps == 8


def test_checkpoint_is_frozen_and_hashable() -> None:
    cp = Checkpoint(step=2, progress=0.5, total_steps=4)
    assert cp == Checkpoint(step=2, progress=0.5, total_steps=4)
    assert hash(cp) == hash(Checkpoint(step=2, progress=0.5, total_steps=4))


def test_drained_job_pairs_checkpoint() -> None:
    dj = DrainedJob(job_id="job-1", checkpoint=Checkpoint(step=3, progress=0.75, total_steps=4))
    assert dj.job_id == "job-1"
    assert dj.checkpoint.step == 3


def test_reused_wire_types_are_common_types() -> None:
    spec = JobSpec(name="x", command=["c"])
    assert spec.priority == 0
    assert JobState.RUNNING.value == "running"
