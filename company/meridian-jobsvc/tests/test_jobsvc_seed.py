"""The cluster's seed: other teams' jobs and schedules, on the cluster before any call."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from jobsvc_support import Clock, cluster, job

from meridian_jobsvc.errors import DependencyError
from meridian_jobsvc.seed import ClusterSeed, SeededJob, load_cluster_seed, load_teams
from meridian_jobsvc.types import Node

NODES = (Node("node-1", cpu_capacity=2, memory_mb=2048), Node("node-2", cpu_capacity=2, memory_mb=2048))


def test_the_seed_replayed_into_an_empty_cluster_runs_one_teams_job_and_queues_the_rest() -> None:
    clock = Clock()
    service, daemons, _ = cluster(*NODES, teams=load_teams(), clock=clock)

    service.seed(load_cluster_seed())
    service.reconcile()

    jobs = [(j.id, j.owner, j.team, j.state.value, j.node, j.principal, j.seed) for j in service.list_jobs()]
    assert jobs == [
        ("job-1", "wen", "eval", "running", "node-1", None, True),
        ("job-2", "wen", "eval", "pending", None, None, True),  # waits for job-1 to succeed
        ("job-3", "tomas", "data-platform", "held", None, None, True),
    ]
    schedules = [(s.id, s.owner, s.principal, s.seed, s.next_run_at) for s in service.list_schedules()]
    assert schedules == [
        ("schedule-1", "ines", None, True, datetime(2026, 10, 22, 9, 10, tzinfo=UTC)),
        ("schedule-2", "wen", None, True, datetime(2026, 10, 23, 2, 17, tzinfo=UTC)),
    ]
    assert set(daemons[0].environments[0]) == {"PYTHONPATH", "PYTHONDONTWRITEBYTECODE"}  # no credential


def test_a_seeded_schedules_run_is_its_owners_seeded_job_holding_no_credential() -> None:
    clock = Clock()
    service, daemons, _ = cluster(*NODES, teams=load_teams(), clock=clock)
    service.seed(load_cluster_seed())
    service.reconcile()

    clock.advance(600)
    service.reconcile()

    run = service.get_job(service.get_schedule("schedule-1").runs[0])
    assert (run.owner, run.team, run.schedule_id, run.principal, run.seed, run.state.value, run.node) == (
        "ines",
        "serving",
        "schedule-1",
        None,
        True,
        "running",
        "node-2",
    )
    assert set(daemons[1].environments[0]) == {"PYTHONPATH", "PYTHONDONTWRITEBYTECODE"}


def test_a_seed_whose_job_waits_on_no_job_is_refused_and_none_of_it_is_kept() -> None:
    service, _, records = cluster(*NODES)
    orphan = SeededJob(owner="wen", job=job("orphan", depends_on=["job-9"]))
    seed = ClusterSeed(teams={}, jobs=[SeededJob(owner="wen", job=job("first")), orphan])

    with pytest.raises(DependencyError):
        service.seed(seed)

    assert (service.list_jobs(), records) == ([], [])
