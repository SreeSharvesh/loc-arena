from __future__ import annotations

import pytest

from meridian_common.jobclient.models import JobSpec
from meridian_jobsvc.errors import DrainError
from meridian_jobsvc.lifecycle import NodeLifecycle
from meridian_jobsvc.scheduler import Scheduler
from meridian_jobsvc.types import Node
from meridian_jobsvc.worker import WorkerRuntime


def _cluster(nodes: list[Node]) -> tuple[Scheduler, WorkerRuntime, NodeLifecycle]:
    sched = Scheduler(nodes)
    worker = WorkerRuntime()
    return sched, worker, NodeLifecycle(sched, worker)


def _place(sched: Scheduler, worker: WorkerRuntime, job_id: str, node: str, *, steps: int) -> None:
    sched.assign(job_id, JobSpec(name=job_id, command=["run"], cpu=1))
    worker.start(job_id, node, steps)


def test_cordon_and_uncordon() -> None:
    sched, _, lc = _cluster([Node("n0")])
    lc.cordon("n0")
    assert lc.is_cordoned("n0")
    assert sched.node("n0").cordoned is True
    lc.uncordon("n0")
    assert not lc.is_cordoned("n0")


def test_cordon_unknown_node_raises() -> None:
    _, _, lc = _cluster([Node("n0")])
    with pytest.raises(DrainError):
        lc.cordon("ghost")


def test_drain_cordons_and_evicts() -> None:
    sched, worker, lc = _cluster([Node("n0", cpu_capacity=4), Node("n1", cpu_capacity=4)])
    _place(sched, worker, "a", "n0", steps=4)
    drained = lc.drain("n0")
    assert [dj.job_id for dj in drained] == ["a"]
    assert lc.is_cordoned("n0")
    assert not worker.is_running("a")


def test_drain_single_job_preserves_checkpoint() -> None:
    sched, worker, lc = _cluster([Node("n0", cpu_capacity=4), Node("n1", cpu_capacity=4)])
    _place(sched, worker, "a", "n0", steps=4)
    worker.step("a")
    worker.step("a")
    drained = lc.drain("n0")
    assert drained[0].checkpoint.step == 2


def test_drain_unknown_node_raises() -> None:
    _, _, lc = _cluster([Node("n0")])
    with pytest.raises(DrainError):
        lc.drain("ghost")


def test_relocate_returns_jobs_own_checkpoint() -> None:
    sched, worker, lc = _cluster([Node("n0", cpu_capacity=4)])
    _place(sched, worker, "a", "n0", steps=4)
    worker.step("a")
    worker.step("a")
    worker.step("a")
    dj = lc.relocate("a")
    assert dj.checkpoint.step == 3
    assert sched.node_of("a") is None


def test_rebalance_moves_excess_off_busy_node() -> None:
    sched, worker, lc = _cluster([Node("n0", cpu_capacity=8), Node("n1", cpu_capacity=8)])
    # Three jobs all crammed onto n0, none on n1.
    for job_id in ("a", "b", "c"):
        _place(sched, worker, job_id, "n0", steps=4)
    moved = lc.rebalance()
    # target per node is ceil(3/2) = 2, so one job is relocated.
    assert len(moved) == 1
    assert len(worker.running_on("n0")) == 2


def test_rebalance_no_op_when_even() -> None:
    sched, worker, lc = _cluster([Node("n0", cpu_capacity=8), Node("n1", cpu_capacity=8)])
    _place(sched, worker, "a", "n0", steps=4)
    _place(sched, worker, "b", "n1", steps=4)
    assert lc.rebalance() == []
