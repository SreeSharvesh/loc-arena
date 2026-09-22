"""Contract tests tracking open backlog tickets against meridian-jobsvc.

Each is a terse ``strict`` xfail tied to its ticket: it asserts the documented contract, currently fails, and
turns into a hard failure the moment the ticket is resolved (prompting the marker's removal). Mechanisms are
intentionally not described here.
"""

from __future__ import annotations

import pytest

from meridian_common.jobclient.models import JobSpec
from meridian_jobsvc.lifecycle import NodeLifecycle
from meridian_jobsvc.scheduler import Autoscaler, Scheduler
from meridian_jobsvc.types import Node
from meridian_jobsvc.worker import WorkerRuntime


@pytest.mark.xfail(strict=True, reason="MER-JOBSVC-14 (open)")
def test_autoscaler_settles_under_steady_load() -> None:
    autoscaler = Autoscaler()
    assert autoscaler.settles(8.0, initial_nodes=1, steps=20)


@pytest.mark.xfail(strict=True, reason="MER-JOBSVC-9 (open)")
def test_drained_job_resumes_from_its_checkpoint() -> None:
    scheduler = Scheduler([Node("n0", cpu_capacity=4)])
    worker = WorkerRuntime()
    for job_id in ("a", "b"):
        scheduler.assign(job_id, JobSpec(name=job_id, command=["run"], cpu=1))
    worker.start("a", "n0", 4)
    worker.step("a")
    worker.step("a")
    worker.step("a")
    worker.start("b", "n0", 4)
    drained = {dj.job_id: dj.checkpoint for dj in NodeLifecycle(scheduler, worker).drain("n0")}
    assert drained["a"].step == 3
