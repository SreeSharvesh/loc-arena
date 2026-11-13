from __future__ import annotations

from collections.abc import Callable

import pytest
from jobsvc_support import CREDENTIAL, OTHER, OWNER, Clock, FakeDaemon, FakeIssuer, cluster, job

from meridian_common.jobclient.models import JobState
from meridian_jobsvc.errors import JobStateConflict, NotJobOwner
from meridian_jobsvc.fleet import NodeUnavailable, ProcessState
from meridian_jobsvc.models import Job, NodeState
from meridian_jobsvc.service import ClusterPolicy, JobService
from meridian_jobsvc.types import Node

GRACE = 30.0
DRAINING = ClusterPolicy(drain_grace_seconds=GRACE)
NODES = (Node("node-1", cpu_capacity=2, memory_mb=2048), Node("node-2", cpu_capacity=2, memory_mb=2048))


def test_a_held_job_waits_until_released_then_starts_in_the_place_it_had() -> None:
    service, (node,), _ = cluster()
    busy = service.submit(job("busy"), OWNER, CREDENTIAL)
    service.reconcile()
    first = service.submit(job("first"), OWNER, CREDENTIAL)
    second = service.submit(job("second"), OWNER, CREDENTIAL)
    service.submit(job("third"), OWNER, CREDENTIAL)

    held = service.hold(first.id, OWNER)
    node.exit(0)
    service.reconcile()  # busy ends, and second starts while first is held
    service.release(first.id, OWNER)
    node.exit(0)
    service.reconcile()

    assert held.state is JobState.HELD
    assert [started for started, _, _ in node.started] == [busy.id, second.id, first.id]


@pytest.mark.parametrize("change", [JobService.hold, JobService.release], ids=["hold", "release"])
@pytest.mark.parametrize("ended", [False, True], ids=["running", "ended"])
def test_a_job_that_has_started_is_neither_held_nor_released(
    change: Callable[[JobService, str, str | None], Job],
    ended: bool,
) -> None:
    service, (node,), _ = cluster()
    submitted = service.submit(job(), OWNER, CREDENTIAL)
    service.reconcile()
    if ended:
        node.exit(0)
        service.reconcile()

    with pytest.raises(JobStateConflict):
        change(service, submitted.id, OWNER)


def test_a_change_with_no_owner_to_check_an_operators_applies_to_anyones_job() -> None:
    service, _, _ = cluster()
    waiting = service.submit(job(), OWNER, CREDENTIAL)

    with pytest.raises(NotJobOwner):
        service.hold(waiting.id, OTHER)
    held = service.hold(waiting.id, None)

    assert held.state is JobState.HELD


def test_a_drained_nodes_job_runs_again_elsewhere_under_its_id_and_identity_as_its_next_attempt() -> None:
    clock, issuer = Clock(), FakeIssuer()
    service, (first, second), records = cluster(*NODES, issuer=issuer, policy=DRAINING, clock=clock)
    submitted = service.submit(job(), OWNER, CREDENTIAL)
    service.reconcile()

    service.drain("node-1")
    clock.advance(GRACE - 1)
    service.reconcile()
    within_grace = first.state
    clock.advance(1)
    service.reconcile()  # the grace is over: its process is stopped
    service.reconcile()  # and once it is gone, the job is queued again and starts on node-2

    moved = service.get_job(submitted.id)
    assert within_grace is ProcessState.RUNNING
    assert (moved.state, moved.node, moved.attempt, moved.principal) == (
        JobState.RUNNING,
        "node-2",
        2,
        submitted.principal,
    )
    assert (second.started[0][0], second.attempts, second.environments) == (
        submitted.id,
        [2],
        first.environments,
    )
    assert len(issuer.asked) == 1  # it runs as the identity issued at submission, never issued another
    assert [record["state"] for record in records] == ["pending", "running", "pending", "running"]


def test_a_drained_node_lets_its_job_end_within_the_grace_and_starts_no_other() -> None:
    clock = Clock()
    service, (node,), _ = cluster(policy=DRAINING, clock=clock)
    finishing = service.submit(job("finishing"), OWNER, CREDENTIAL)
    service.reconcile()
    waiting = service.submit(job("waiting"), OWNER, CREDENTIAL)

    service.drain("node-1")
    node.exit(0)
    service.reconcile()

    states = (service.get_job(finishing.id).state, service.get_job(waiting.id).state)
    assert (states, node.attempts) == ((JobState.SUCCEEDED, JobState.PENDING), [1])
    assert [(found.id, found.state) for found in service.list_nodes()] == [("node-1", NodeState.DRAINED)]


class DeafToStop(FakeDaemon):
    """A node that does not answer when asked to stop its job."""

    def stop(self) -> None:
        raise NodeUnavailable(f"{self.node.node_id} does not answer")


def test_a_drained_nodes_job_whose_process_exits_0_before_it_is_stopped_has_succeeded() -> None:
    clock, node = Clock(), DeafToStop(NODES[0])
    service = JobService([node], lambda line: None, FakeIssuer(), DRAINING, clock=clock)
    submitted = service.submit(job(), OWNER, CREDENTIAL)
    service.reconcile()
    service.drain("node-1")
    clock.advance(GRACE)
    service.reconcile()  # the grace is over, and its node does not answer the stop

    node.exit(0)
    service.reconcile()

    ended = service.get_job(submitted.id)
    assert (ended.state, ended.exit_code, ended.attempt) == (JobState.SUCCEEDED, 0, 1)
