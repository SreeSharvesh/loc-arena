from __future__ import annotations

import pytest

from meridian_jobsvc.errors import JobNotFound
from meridian_jobsvc.types import Checkpoint
from meridian_jobsvc.worker import WorkerRuntime


def test_start_step_until_finished() -> None:
    w = WorkerRuntime()
    w.start("a", "n0", 3)
    assert w.is_running("a")
    for _ in range(3):
        w.step("a")
    assert w.is_finished("a")
    assert not w.is_running("a")
    assert w.progress("a") == 1.0


def test_progress_advances_per_step() -> None:
    w = WorkerRuntime()
    w.start("a", "n0", 4)
    w.step("a")
    assert w.progress("a") == 0.25
    w.step("a")
    assert w.progress("a") == 0.5


def test_checkpoint_reflects_completed_steps() -> None:
    w = WorkerRuntime()
    w.start("a", "n0", 4)
    w.step("a")
    w.step("a")
    cp = w.checkpoint("a")
    assert cp.step == 2
    assert cp.progress == 0.5
    assert cp.total_steps == 4


def test_heartbeat_uses_injected_clock() -> None:
    ticks = iter([10, 20, 30])
    w = WorkerRuntime(clock=lambda: next(ticks))
    w.start("a", "n0", 3)  # heartbeat 10
    assert w.heartbeat("a") == 10
    w.step("a")  # heartbeat 20
    assert w.heartbeat("a") == 20


def test_resume_continues_from_checkpoint() -> None:
    w = WorkerRuntime()
    w.resume("a", "n1", 4, Checkpoint(step=2, progress=0.5, total_steps=4))
    assert w.progress("a") == 0.5
    assert w.node_of("a") == "n1"
    w.step("a")
    assert w.checkpoint("a").step == 3


def test_release_returns_checkpoint_and_evicts() -> None:
    w = WorkerRuntime()
    w.start("a", "n0", 4)
    w.step("a")
    cp = w.release("a")
    assert cp.step == 1
    assert not w.is_running("a")


def test_running_on_lists_jobs_in_order() -> None:
    w = WorkerRuntime()
    w.start("b", "n0", 3)
    w.start("a", "n0", 3)
    w.start("c", "n1", 3)
    assert w.running_on("n0") == ["a", "b"]
    assert w.running_on("n1") == ["c"]


def test_node_resume_checkpoint_matches_single_job() -> None:
    w = WorkerRuntime()
    w.start("a", "n0", 4)
    w.step("a")
    w.step("a")
    assert w.node_resume_checkpoint("n0") == w.checkpoint("a")


def test_step_unknown_job_raises() -> None:
    w = WorkerRuntime()
    with pytest.raises(JobNotFound):
        w.step("ghost")


def test_finished_job_stops_advancing() -> None:
    w = WorkerRuntime()
    w.start("a", "n0", 1)
    w.step("a")
    assert w.is_finished("a")
    w.step("a")  # no-op, does not raise
    assert w.progress("a") == 1.0
