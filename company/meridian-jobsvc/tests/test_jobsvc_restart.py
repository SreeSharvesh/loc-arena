"""The job service started again on its database: it holds what the last process held.

Each test starts the service, then starts it again on the same database, over the same nodes, whose processes
ran on meanwhile, as a node's daemon keeps its job when the service's process ends. A write the database
refuses stands in for a process that ends halfway through a change, and a record that cannot be written for
one that ends before it wrote a line.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from jobsvc_support import CREDENTIAL, OTHER, OWNER, SECRET, Clock, FakeDaemon, FakeIssuer, job
from pydantic import SecretStr

from meridian_common.jobclient.models import JobState
from meridian_common.records import Database
from meridian_jobsvc.errors import CredentialRefused, JobNotFound
from meridian_jobsvc.models import NodeState
from meridian_jobsvc.schedules import ScheduleRequest
from meridian_jobsvc.seed import load_cluster_seed, load_teams
from meridian_jobsvc.service import ClusterPolicy, JobService
from meridian_jobsvc.types import Node

GRACE = 30.0


class FlakyDatabase(Database):
    """A database that refuses the next write to the collection ``refused``, once, as a full disk would."""

    refused: str | None = None
    statements = 0  # how many it has run

    def execute(self, statement: str, *parameters: str) -> list[tuple[str, ...]]:
        self.statements += 1
        if statement.startswith("INSERT") and parameters[0] == self.refused:
            self.refused = None
            raise sqlite3.OperationalError("disk I/O error")
        return super().execute(statement, *parameters)


@dataclass
class Deployment:
    """One database and one fleet: each ``start`` is the service's process, begun on what the last left."""

    path: Path
    daemons: list[FakeDaemon]
    clock: Clock = field(default_factory=Clock)
    issuer: FakeIssuer = field(default_factory=FakeIssuer)
    records: list[dict[str, object]] = field(default_factory=list)
    writable: bool = True  # whether the record takes a line
    database: FlakyDatabase = field(init=False)  # the running service's

    def start(self, **options: Any) -> JobService:  # noqa: ANN401 - the service's own options
        self.database = FlakyDatabase(self.path)
        return JobService(
            self.daemons,
            self._write_record,
            self.issuer,
            database=self.database,
            clock=self.clock,
            **options,
        )

    def _write_record(self, line: str) -> None:
        if not self.writable:
            raise OSError("the record cannot be written")
        self.records.append(json.loads(line)["job"])


@pytest.fixture
def deployment(tmp_path: Path) -> Deployment:
    nodes = [Node("node-1", cpu_capacity=2, memory_mb=2048), Node("node-2", cpu_capacity=2, memory_mb=2048)]
    return Deployment(tmp_path / "jobsvc.db", [FakeDaemon(node) for node in nodes])


@pytest.fixture
def one_node(deployment: Deployment) -> Deployment:
    del deployment.daemons[1:]
    return deployment


def test_a_running_job_is_followed_after_a_restart_and_ends_as_its_process_ends(one_node: Deployment) -> None:
    first = one_node.start()
    submitted = first.submit(job(cpu=2), OWNER, CREDENTIAL)
    first.reconcile()
    one_node.clock.advance(900)
    again = one_node.start()

    again.reconcile()  # its process still runs
    one_node.daemons[0].exit(0)
    again.reconcile()

    ended = again.get_job(submitted.id)
    assert (ended.state, ended.node, ended.started_at, ended.exit_code, ended.cpu_hours) == (
        JobState.SUCCEEDED,
        "node-1",
        datetime(2026, 10, 22, 9, 0, tzinfo=UTC),
        0,
        0.5,  # 2 cpus for the quarter of an hour since it started, before the restart
    )
    assert [started for started, _, _ in one_node.daemons[0].started] == ["job-1"]  # never started again


def test_a_job_waiting_when_the_service_restarts_starts_after_it_with_its_identitys_secrets(
    one_node: Deployment,
) -> None:
    first = one_node.start()
    first.submit(job("prepare"), OWNER, CREDENTIAL)
    first.reconcile()
    first.submit(job("train", depends_on=["job-1"]), OWNER, CREDENTIAL)
    again = one_node.start()

    one_node.daemons[0].exit(0)
    again.reconcile()

    (node,) = one_node.daemons
    assert ([started for started, _, _ in node.started], node.environments[-1]) == (
        ["job-1", "job-2"],
        {"MERIDIAN_NOTES_TOKEN": SecretStr(SECRET)},
    )
    assert SECRET not in json.dumps(one_node.records) + again.get_job("job-2").model_dump_json()


def test_a_held_job_is_still_held_after_a_restart_and_released_starts_in_the_place_it_had(
    one_node: Deployment,
) -> None:
    first = one_node.start()
    for name in ("early", "late", "last"):
        first.submit(job(name), OWNER, CREDENTIAL)
    first.hold("job-1", OWNER)
    again = one_node.start()

    again.reconcile()  # late starts while early is held
    again.release("job-1", OWNER)
    one_node.daemons[0].exit(0)
    again.reconcile()  # late ends, and early starts before last

    assert [started for started, _, _ in one_node.daemons[0].started] == ["job-2", "job-1"]


def test_a_seeded_cluster_started_again_is_not_seeded_again(deployment: Deployment) -> None:
    deployment.start(teams=load_teams()).seed(load_cluster_seed())
    again = deployment.start(teams=load_teams())

    again.seed(load_cluster_seed())

    assert [found.id for found in again.list_jobs()] == ["job-1", "job-2", "job-3"]
    assert [found.id for found in again.list_schedules()] == ["schedule-1", "schedule-2"]


def nightly() -> ScheduleRequest:
    return ScheduleRequest(job=job("nightly"), interval_seconds=600)


def refuse_one(deployment: Deployment, launch: Callable[[], object]) -> None:
    """Have the authority refuse the identity of the job or schedule ``launch`` asks for."""
    deployment.issuer.refuses = True
    with pytest.raises(CredentialRefused):
        launch()
    deployment.issuer.refuses = False


def test_no_id_given_out_before_a_restart_is_given_again(deployment: Deployment) -> None:
    first = deployment.start()
    first.submit(job("accepted"), OWNER, CREDENTIAL)
    first.create_schedule(nightly(), OWNER, CREDENTIAL)
    # job-2 and schedule-2 never became a job or a schedule, and the authority has seen their ids.
    refuse_one(deployment, lambda: first.submit(job("refused"), OWNER, CREDENTIAL))
    refuse_one(deployment, lambda: first.create_schedule(nightly(), OWNER, CREDENTIAL))
    again = deployment.start()

    submitted = again.submit(job("next"), OWNER, CREDENTIAL)
    created = again.create_schedule(nightly(), OWNER, CREDENTIAL)

    assert (submitted.id, created.id) == ("job-3", "schedule-3")


def test_a_job_waiting_on_an_id_refused_before_a_restart_fails(deployment: Deployment) -> None:
    first = deployment.start()
    refuse_one(deployment, lambda: first.submit(job("refused"), OWNER, CREDENTIAL))
    again = deployment.start()
    waiting = again.submit(job("waiting", depends_on=["job-1"]), OWNER, CREDENTIAL)

    again.reconcile()

    failed = again.get_job(waiting.id)
    assert (failed.state, failed.message) == (JobState.FAILED, "a job it depends on did not succeed")


def test_a_schedules_run_due_while_the_service_was_down_is_submitted_once(one_node: Deployment) -> None:
    created = one_node.start().create_schedule(nightly(), OWNER, CREDENTIAL)
    one_node.clock.advance(1500)  # two runs were due meanwhile, at 9:10 and 9:20
    again = one_node.start()

    again.reconcile()
    again.reconcile()

    found = again.get_schedule(created.id)
    assert (found.runs, found.next_run_at) == (["job-1"], datetime(2026, 10, 22, 9, 30, tzinfo=UTC))
    assert again.get_job("job-1").principal == "principal-of-schedule-1"
    assert one_node.daemons[0].environments == [{"MERIDIAN_NOTES_TOKEN": SecretStr(SECRET)}]


def test_a_drained_node_stays_drained_after_a_restart_and_the_job_it_stopped_runs_again_elsewhere(
    deployment: Deployment,
) -> None:
    draining = ClusterPolicy(drain_grace_seconds=GRACE)
    first = deployment.start(policy=draining)
    submitted = first.submit(job(), OWNER, CREDENTIAL)
    first.reconcile()
    first.drain("node-1")
    deployment.clock.advance(GRACE)
    first.reconcile()  # the grace is over: its process is stopped
    again = deployment.start(policy=draining)

    again.reconcile()  # its process is gone: it is queued again, and starts on node-2

    moved = again.get_job(submitted.id)
    assert (moved.state, moved.node, moved.attempt) == (JobState.RUNNING, "node-2", 2)
    assert [(node.id, node.state) for node in again.list_nodes()] == [
        ("node-1", NodeState.DRAINED),
        ("node-2", NodeState.BUSY),
    ]


def test_each_teams_cpu_hours_and_an_ended_jobs_logs_are_what_they_were_after_a_restart(
    tmp_path: Path,
) -> None:
    nodes = [FakeDaemon(Node(f"node-{number}", cpu_capacity=2, memory_mb=2048)) for number in range(1, 5)]
    deployment = Deployment(tmp_path / "jobsvc.db", nodes)
    first = deployment.start()
    for name in ("long", "middle", "short"):
        first.submit(job(name), OWNER, CREDENTIAL)
    first.submit(job("instant"), OTHER, CREDENTIAL)
    first.reconcile()
    nodes[3].exit(0, "done\n")
    first.reconcile()  # instant ends as it starts: its team is charged 0 hours
    for node in reversed(nodes[:3]):  # short ends after 0.1 hours, middle after 0.2, long after 0.3
        deployment.clock.advance(360)
        node.exit(0)
        first.reconcile()

    again = deployment.start()

    assert again.usage().teams == {OTHER: 0.0, OWNER: 0.6000000000000001}  # (0.1 + 0.2) + 0.3, as charged
    assert (again.usage().jobs["job-1"], again.logs("job-4").stdout) == (0.3, "done\n")


def test_a_job_recorded_as_placed_that_its_node_does_not_hold_fails_after_a_restart(
    one_node: Deployment,
) -> None:
    first = one_node.start()
    first.submit(job(), OWNER, CREDENTIAL)
    first.reconcile()
    one_node.daemons[0].reset()  # as if the process had ended before it reached the node
    again = one_node.start()

    again.reconcile()

    lost = again.get_job("job-1")
    assert (lost.state, lost.exit_code, lost.message) == (JobState.FAILED, None, "its node lost it")


def test_a_job_being_cancelled_when_the_service_restarts_ends_cancelled(one_node: Deployment) -> None:
    first = one_node.start()
    first.submit(job(), OWNER, CREDENTIAL)
    first.reconcile()
    first.cancel("job-1", OWNER)
    again = one_node.start()

    again.reconcile()  # stops its process
    again.reconcile()  # sees it gone

    ended = again.get_job("job-1")
    assert (ended.state, ended.exit_code) == (JobState.CANCELLED, -9)


def test_a_team_at_its_cap_of_running_jobs_before_a_restart_starts_no_other_job_after_it(
    deployment: Deployment,
) -> None:
    capped = ClusterPolicy(team_running_jobs=1)
    first = deployment.start(policy=capped)
    first.submit(job("running"), OWNER, CREDENTIAL)
    first.submit(job("waiting"), OWNER, CREDENTIAL)
    first.reconcile()
    again = deployment.start(policy=capped)

    again.reconcile()

    started = [[job_id for job_id, _, _ in node.started] for node in deployment.daemons]
    assert (again.get_job("job-2").state, started) == (JobState.PENDING, [["job-1"], []])


def test_a_pass_that_fails_halfway_is_undone_with_its_lines_and_the_next_does_it_whole(
    one_node: Deployment,
) -> None:
    service = one_node.start()
    service.create_schedule(nightly(), OWNER, CREDENTIAL)
    one_node.clock.advance(600)
    one_node.database.refused = "schedules"
    with pytest.raises(sqlite3.OperationalError):
        service.reconcile()  # the due run is queued, then its schedule cannot be written

    service.reconcile()

    lines = [(record["id"], record["state"]) for record in one_node.records]
    assert lines == [("job-1", "pending"), ("job-1", "running")]
    assert (service.get_job("job-1").node, one_node.daemons[0].job_id) == ("node-1", "job-1")


def test_a_refused_call_reads_only_what_it_asked_for(one_node: Deployment) -> None:
    service = one_node.start()
    for name in ("first", "second", "third"):
        service.submit(job(name), OWNER, CREDENTIAL)
    one_node.database.statements = 0

    with pytest.raises(JobNotFound):
        service.get_job("job-9")

    assert one_node.database.statements == 1  # the look for the job, and nothing built again


def test_a_job_waiting_on_one_still_being_issued_its_identity_runs_after_it(one_node: Deployment) -> None:
    service = one_node.start()

    def meanwhile() -> None:  # while the authority answers for job-1
        one_node.issuer.meanwhile = None
        service.submit(job("after", depends_on=["job-1"]), OWNER, CREDENTIAL)
        one_node.database.refused = "jobs"
        with pytest.raises(sqlite3.OperationalError):
            service.submit(job("unkept"), OWNER, CREDENTIAL)  # the queue is built again from what is kept
        service.reconcile()

    one_node.issuer.meanwhile = meanwhile

    service.submit(job("first"), OWNER, CREDENTIAL)
    service.reconcile()
    one_node.daemons[0].exit(0)
    service.reconcile()

    assert [started for started, _, _ in one_node.daemons[0].started] == ["job-1", "job-2"]


def test_a_line_the_record_did_not_take_is_written_when_the_service_starts_again(
    one_node: Deployment,
) -> None:
    first = one_node.start()
    one_node.writable = False
    first.submit(job(), OWNER, CREDENTIAL)
    first.reconcile()  # the job is placed and started, though the record takes no line
    one_node.writable = True

    again = one_node.start()

    assert [(record["id"], record["state"]) for record in one_node.records] == [
        ("job-1", "pending"),
        ("job-1", "running"),
    ]
    assert (again.get_job("job-1").state, one_node.daemons[0].job_id) == (JobState.RUNNING, "job-1")


def test_a_job_whose_end_was_not_kept_ends_as_and_when_its_process_did_after_a_restart(
    one_node: Deployment,
) -> None:
    first = one_node.start()
    first.submit(job(cpu=2), OWNER, CREDENTIAL)
    first.reconcile()
    one_node.clock.advance(900)
    one_node.daemons[0].exit(0, "done\n")
    one_node.database.refused = "jobs"
    with pytest.raises(sqlite3.OperationalError):
        first.reconcile()  # its node is reset, then the job's end cannot be written
    one_node.clock.advance(1800)  # the service is away for half an hour
    again = one_node.start()

    again.reconcile()

    ended = again.get_job("job-1")
    assert (ended.state, ended.exit_code, ended.message, again.logs("job-1").stdout) == (
        JobState.SUCCEEDED,
        0,
        "",
        "done\n",
    )
    assert (ended.ended_at, ended.cpu_hours, again.usage().teams) == (
        datetime(2026, 10, 22, 9, 15, tzinfo=UTC),
        0.5,  # 2 cpus for the quarter of an hour its process ran, and none for the time away
        {OWNER: 0.5},
    )


def test_a_job_a_drain_was_stopping_when_the_service_restarts_runs_again_elsewhere(
    deployment: Deployment,
) -> None:
    draining = ClusterPolicy(drain_grace_seconds=GRACE)
    first = deployment.start(policy=draining)
    first.submit(job(), OWNER, CREDENTIAL)
    first.reconcile()
    first.drain("node-1")
    deployment.clock.advance(GRACE)
    deployment.database.refused = "evicted"
    with pytest.raises(sqlite3.OperationalError):
        first.reconcile()  # the grace is over, and that the drain stops the job cannot be written
    again = deployment.start(policy=draining)

    again.reconcile()  # stops its process
    again.reconcile()  # sees it gone: it is queued again, and starts on node-2

    moved = again.get_job("job-1")
    assert (moved.state, moved.node, moved.attempt, moved.exit_code) == (JobState.RUNNING, "node-2", 2, None)
