from __future__ import annotations

import pytest

from meridian_common.jobclient.models import JobState
from meridian_jobsvc.errors import JobNotFound
from meridian_jobsvc.service import JobService
from meridian_jobsvc.types import Node


def _spec(name: str, **kw: object) -> dict[str, object]:
    base: dict[str, object] = {"name": name, "command": ["run", name]}
    base.update(kw)
    return base


def test_submit_then_poll_to_success() -> None:
    svc = JobService()
    jid = svc.submit(_spec("eval", labels={"steps": "2"}))["job_id"]
    assert svc.status(jid)["state"] == JobState.RUNNING.value
    final = svc.status(jid)
    assert final["state"] == JobState.SUCCEEDED.value
    assert final["exit_code"] == 0
    assert final["progress"] == 1.0


def test_run_to_idle_drives_all_jobs_terminal() -> None:
    svc = JobService()
    a = svc.submit(_spec("a", labels={"steps": "2"}))["job_id"]
    b = svc.submit(_spec("b", labels={"steps": "3"}))["job_id"]
    svc.run_to_idle()
    assert svc.status(a)["state"] == JobState.SUCCEEDED.value
    assert svc.status(b)["state"] == JobState.SUCCEEDED.value


def test_dependency_runs_after_parent() -> None:
    svc = JobService()
    prep = svc.submit(_spec("prep", labels={"steps": "2"}))["job_id"]
    child = svc.submit(_spec("child", depends_on=[prep], labels={"steps": "2"}))["job_id"]
    svc.run_to_idle()
    assert svc.status(prep)["state"] == JobState.SUCCEEDED.value
    assert svc.status(child)["state"] == JobState.SUCCEEDED.value


def test_dependency_failure_blocks_child() -> None:
    svc = JobService()
    prep = svc.submit(_spec("prep", labels={"steps": "5"}))["job_id"]
    child = svc.submit(_spec("child", depends_on=[prep]))["job_id"]
    svc.cancel(prep)
    svc.run_to_idle()
    assert svc.status(prep)["state"] == JobState.CANCELLED.value
    assert svc.status(child)["state"] == JobState.FAILED.value


def test_unschedulable_job_fails() -> None:
    svc = JobService(node_count=1, per_node_cpu=2.0)
    jid = svc.submit(_spec("big", cpu=8.0))["job_id"]
    status = svc.status(jid)
    assert status["state"] == JobState.FAILED.value
    assert status["exit_code"] == 2


def test_priority_orders_placement_under_scarcity() -> None:
    svc = JobService(node_count=1, per_node_cpu=1.0)
    low = svc.submit(_spec("low", cpu=1.0, priority=1, labels={"steps": "3"}))["job_id"]
    high = svc.submit(_spec("high", cpu=1.0, priority=5, labels={"steps": "3"}))["job_id"]
    svc.tick()
    assert svc._status_of(high)["state"] == JobState.RUNNING.value
    assert svc._status_of(low)["state"] == JobState.PENDING.value


def test_cancel_running_job() -> None:
    svc = JobService()
    jid = svc.submit(_spec("x", labels={"steps": "10"}))["job_id"]
    svc.tick()
    result = svc.cancel(jid)
    assert result["state"] == JobState.CANCELLED.value
    assert not svc.worker.is_running(jid)


def test_drain_node_resumes_job_from_checkpoint() -> None:
    svc = JobService(nodes=[Node("node-0", cpu_capacity=4), Node("node-1", cpu_capacity=4)])
    jid = svc.submit(_spec("x", cpu=1.0, labels={"steps": "6"}))["job_id"]
    svc.tick()  # placed on node-0, stepped once
    svc.tick()  # stepped twice
    assert svc.job_checkpoint(jid).step == 2
    moved = svc.drain_node("node-0")
    assert moved == [jid]
    assert svc.worker.node_of(jid) == "node-1"
    assert svc.job_checkpoint(jid).step == 2  # single job per node: checkpoint preserved
    svc.run_to_idle()
    assert svc.status(jid)["state"] == JobState.SUCCEEDED.value


def test_ledger_charged_on_success() -> None:
    svc = JobService()
    svc.submit(_spec("x", cpu=2.0, labels={"steps": "3", "identity": "team-a"}))
    svc.run_to_idle()
    spent = svc.ledger.spent("team-a")
    assert spent.compute == 6.0  # cpu 2.0 * 3 steps


def test_logs_capture_lifecycle() -> None:
    svc = JobService()
    jid = svc.submit(_spec("x", labels={"steps": "2"}))["job_id"]
    svc.run_to_idle()
    logs = svc.logs(jid)
    assert any("started" in line for line in logs)
    assert any("succeeded" in line for line in logs)


def test_unknown_job_status_raises() -> None:
    svc = JobService()
    with pytest.raises(JobNotFound):
        svc.status("job-404")
