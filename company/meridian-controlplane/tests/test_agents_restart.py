"""The agents service across a restart of its process: what it holds again, and what it does about a run.

Most tests start the service's records twice on one database file, as two processes of one container do. The
first is abandoned as a process that ends is: its threads never go on, since its runner holds every command.
The second finds the runner as a new process does, through a client of its own. The last tests are of a
worker and its runner in one process: a stop beside a resume, the first reset, a runner that does not answer.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from test_agents_service import (
    IDENTITIES,
    READER,
    STARTING_RIGHTS,
    HeldRunner,
    RunningService,
    StandInAuthority,
)

from meridian_common.records import Database
from meridian_controlplane.agents import (
    AgentDefinition,
    AgentIdentity,
    AgentRegistration,
    AgentRegistry,
    Execution,
    ExecutionRequest,
    Executions,
    RunnerResult,
    Schedule,
    ScheduleRequest,
    Schedules,
    TelemetryConfiguration,
    TelemetryOverrides,
    create_agents_service_app,
    load_seeded_agents,
    load_seeded_schedules,
    load_seeded_telemetry,
)
from meridian_controlplane.agents.executions import ResumeRequest

LEAD = "lead"
TRIAGER = AgentRegistration(name="triager", model="provider/model", system_prompt="Triage the new tickets.")
TRIAGE = ExecutionRequest(agent="ticket-triager", input="Triage today's tickets.")
WAIT_SECONDS = 10.0
NEVER_SECONDS = 3600.0  # a held command outlasts its test: an abandoned service's worker never goes on
BLOCKED_SECONDS = 2 * WAIT_SECONDS  # a blocked call outlasts every wait of a test, and no test hangs on it


@dataclass
class FoundRunner(HeldRunner):
    """The runner as a service that starts again finds it: what it is asked, and what its checkpoint gives."""

    packed: RunnerResult = field(
        default_factory=lambda: RunnerResult(returncode=0, stdout="what it held", stderr=""),
    )
    asked: list[str] = field(default_factory=list)

    def end_processes(self, timeout_seconds: float) -> None:
        self.asked.append("end_processes")
        super().end_processes(timeout_seconds)

    def checkpoint(self, timeout_seconds: float) -> RunnerResult:
        self.asked.append("checkpoint")
        return self.packed

    def reset(self) -> None:
        self.asked.append("reset")
        super().reset()


@dataclass
class BlockedRunner(FoundRunner):
    """A found runner whose call ``blocked`` waits until the test releases it, as a slow or dead runner's."""

    blocked: str = "end_processes"
    released: threading.Event = field(default_factory=threading.Event)

    def _wait_in(self, call: str) -> None:
        if call == self.blocked:
            self.released.wait(BLOCKED_SECONDS)

    def end_processes(self, timeout_seconds: float) -> None:
        self._wait_in("end_processes")
        super().end_processes(timeout_seconds)

    def restore(self, checkpoint: str, timeout_seconds: float) -> RunnerResult:
        self._wait_in("restore")
        return super().restore(checkpoint, timeout_seconds)

    def reset(self) -> None:
        self._wait_in("reset")
        super().reset()


@dataclass
class UnansweringRunner(HeldRunner):
    """A runner that does not answer the first reset after a command, and answers from then on."""

    unanswered: bool = False

    def reset(self) -> None:
        super().reset()
        if self.commands and not self.unanswered:
            self.unanswered = True
            raise ConnectionError("runner-1 did not answer")


@dataclass
class Service:
    """The service's records, the lines of its audit record, and each call its authority decided."""

    registry: AgentRegistry
    executions: Executions
    schedules: Schedules
    audit_lines: list[str]
    authorized: list[tuple[str, str]]

    def execution_states(self) -> list[str]:
        """The state of each execution state line of the audit record, in order."""
        return [line["state"] for line in map(json.loads, self.audit_lines) if "state" in line]

    def wait_for_state(self, execution_id: str, state: str) -> Execution:
        deadline = time.monotonic() + WAIT_SECONDS
        while (execution := self.executions.get_execution(execution_id)).state != state:
            assert time.monotonic() < deadline, execution
            time.sleep(0.01)
        return execution


def wait_until(reached: Callable[[], bool]) -> None:
    """Wait, at most ``WAIT_SECONDS``, until ``reached`` says so."""
    deadline = time.monotonic() + WAIT_SECONDS
    while not reached():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def start_service(directory: Path, runner: HeldRunner, database: Database | None = None) -> Service:
    """The service's records on the database in ``directory``, from its seed, as its process starts them."""
    database = database or Database(directory / "agents.db")
    audit_lines: list[str] = []
    authorized: list[tuple[str, str]] = []
    registry = AgentRegistry(load_seeded_agents(), database)
    executions = Executions(
        registry,
        [runner],
        NEVER_SECONDS,
        audit_lines.append,
        database,
        load_seeded_telemetry(),
    )
    schedules = Schedules(
        executions,
        lambda token, operation: authorized.append((token, operation)),
        audit_lines.append,
        database,
        load_seeded_schedules(),
    )
    return Service(registry, executions, schedules, audit_lines, authorized)


def test_a_registry_started_again_holds_each_agent_with_its_versions_and_its_identity(tmp_path: Path) -> None:
    first = start_service(tmp_path, HeldRunner()).registry
    identity = AgentIdentity(
        principal="job-7",
        secrets={"notes": SecretStr("notes-secret")},
        badge=SecretStr("signed-badge"),
    )
    first.register(TRIAGER, LEAD, lambda: identity)
    first.add_version(
        "triager",
        AgentDefinition(model="provider/model", system_prompt="Triage and label."),
        LEAD,
    )
    first.retire("ticket-triager")

    again = start_service(tmp_path, HeldRunner()).registry

    held = again.get_identity("triager")
    assert held is not None
    assert (held.principal, held.secrets["notes"].get_secret_value(), held.badge) == (
        "job-7",
        "notes-secret",
        SecretStr("signed-badge"),
    )
    assert [(agent.name, agent.identity, agent.retired) for agent in again.list_agents()] == [
        ("eval-summarizer", None, False),
        ("ticket-triager", None, True),
        ("triager", "job-7", False),
    ]
    assert [(version.number, version.system_prompt) for version in again.list_versions("triager")] == [
        (1, "Triage the new tickets."),
        (2, "Triage and label."),
    ]


def test_a_service_started_again_holds_an_ended_execution_and_numbers_the_next_after_it(
    tmp_path: Path,
) -> None:
    runner = HeldRunner()
    first = start_service(tmp_path, runner)
    first.executions.start(TRIAGE, LEAD)
    runner.release(1)
    first.wait_for_state("execution-1", "completed")
    again = start_service(tmp_path, HeldRunner())

    started = again.executions.start(TRIAGE, LEAD)

    ended = again.executions.get_execution("execution-1")
    assert (ended.state, ended.output, ended.runner, started.id) == (
        "completed",
        "done\n",
        "runner-1",
        "execution-2",
    )


def test_an_execution_stopped_before_the_service_started_again_resumes_from_its_checkpoint(
    tmp_path: Path,
) -> None:
    first = start_service(tmp_path, HeldRunner())
    first.executions.start(TRIAGE, LEAD)
    first.wait_for_state("execution-1", "running")
    first.executions.stop("execution-1")
    runner = HeldRunner()
    again = start_service(tmp_path, runner)

    again.executions.resume("execution-1", ResumeRequest())
    runner.release(1)

    again.wait_for_state("execution-1", "completed")
    assert runner.restored == ["checkpoint after 1"]


def test_an_execution_running_when_the_service_ended_is_checkpointed_and_resumes_once_it_starts_again(
    tmp_path: Path,
) -> None:
    first = start_service(tmp_path, HeldRunner())
    first.registry.register(TRIAGER, LEAD, lambda: None)
    first.executions.start(ExecutionRequest(agent="triager", input="Triage today's tickets."), LEAD)
    first.wait_for_state("execution-1", "running")
    runner = FoundRunner()

    again = start_service(tmp_path, runner)
    runner.release(1)

    ended = again.wait_for_state("execution-1", "completed")
    assert (runner.asked[:3], runner.restored) == (["end_processes", "checkpoint", "reset"], ["what it held"])
    assert runner.commands == [
        [
            "pi-on-gateway",
            "provider/model",
            "Triage the new tickets.",
            "Triage today's tickets.",
            "This run was stopped and has resumed. Continue the task.",
        ],
    ]
    assert (again.execution_states(), ended.output, ended.error_output) == (
        ["queued", "running", "completed"],
        "done\n",
        "the service did not record how this run ended\n",
    )


def test_an_execution_whose_restore_the_service_ended_in_resumes_from_the_checkpoint_it_still_holds(
    tmp_path: Path,
) -> None:
    packed = RunnerResult(returncode=0, stdout="packed at its stop", stderr="")
    first = start_service(tmp_path, BlockedRunner(blocked="restore", packed=packed))
    first.executions.start(TRIAGE, LEAD)
    first.wait_for_state("execution-1", "running")
    first.executions.stop("execution-1")
    first.executions.resume("execution-1", ResumeRequest())
    first.wait_for_state("execution-1", "running")
    runner = FoundRunner()

    again = start_service(tmp_path, runner)
    runner.release(1)

    again.wait_for_state("execution-1", "completed")
    assert (runner.asked[:2], runner.restored) == (["end_processes", "reset"], ["packed at its stop"])


def test_an_execution_stopped_while_the_service_recovers_it_stays_stopped_and_resumes_when_asked(
    tmp_path: Path,
) -> None:
    first = start_service(tmp_path, HeldRunner())
    first.executions.start(TRIAGE, LEAD)
    first.wait_for_state("execution-1", "running")
    runner = BlockedRunner()
    again = start_service(tmp_path, runner)
    threading.Timer(0.3, runner.released.set).start()  # the stop is asked first: recovery waits meanwhile

    stopped = again.executions.stop("execution-1")
    again.executions.resume("execution-1", ResumeRequest())
    runner.release(1)

    again.wait_for_state("execution-1", "completed")
    assert (stopped.state, again.execution_states(), runner.restored) == (
        "stopped",
        ["stopped", "queued", "running", "completed"],
        ["what it held"],
    )


def test_an_execution_recovered_by_a_process_that_ended_at_once_is_completed_by_the_next(
    tmp_path: Path,
) -> None:
    first = start_service(tmp_path, HeldRunner())
    first.executions.start(TRIAGE, LEAD)
    first.wait_for_state("execution-1", "running")
    second = start_service(tmp_path, BlockedRunner(blocked="reset"))  # it ends once recovery has written
    second.wait_for_state("execution-1", "queued")
    runner = FoundRunner()

    third = start_service(tmp_path, runner)
    runner.release(1)

    third.wait_for_state("execution-1", "completed")
    assert (runner.restored, runner.commands[0][-1]) == (
        ["what it held"],
        "This run was stopped and has resumed. Continue the task.",
    )


def test_the_service_answers_while_a_runner_it_recovers_does_not(
    tmp_path: Path,
    serve: Callable[[FastAPI], str],
) -> None:
    first = start_service(tmp_path, HeldRunner())
    first.executions.start(TRIAGE, LEAD)
    first.wait_for_state("execution-1", "running")

    runner = BlockedRunner()

    again = start_service(tmp_path, runner)
    authority = StandInAuthority(IDENTITIES, STARTING_RIGHTS)
    app = create_agents_service_app(again.registry, again.audit_lines.append, authority, again.executions)
    status, execution = RunningService(serve(app)).call_as(READER, "GET", "/executions/execution-1")

    # The runner has not answered yet: its first call still waits.
    assert (status, execution["state"], again.execution_states(), runner.asked) == (
        HTTPStatus.OK,
        "running",
        [],
        [],
    )


def test_a_stop_still_returning_does_not_end_the_run_that_resumes_its_execution(tmp_path: Path) -> None:
    runner = HeldRunner()
    service = start_service(tmp_path, runner)
    service.executions.start(TRIAGE, LEAD)
    service.wait_for_state("execution-1", "running")
    stopping = threading.Thread(target=service.executions.stop, args=("execution-1",))
    stopping.start()
    service.wait_for_state("execution-1", "stopped")

    service.executions.resume("execution-1", ResumeRequest())
    service.wait_for_state("execution-1", "running")
    stopping.join()
    runner.release(2)

    ended = service.wait_for_state("execution-1", "completed")
    assert ended.output == "done\n"


def test_a_runner_is_reset_before_the_first_execution_a_process_runs_on_it(tmp_path: Path) -> None:
    runner = FoundRunner()
    service = start_service(tmp_path, runner)

    service.executions.start(TRIAGE, LEAD)

    service.wait_for_state("execution-1", "running")
    assert runner.asked == ["reset"]


def test_a_runner_that_does_not_answer_a_reset_runs_a_later_execution_once_it_answers(
    tmp_path: Path,
) -> None:
    unanswered = RunnerResult(returncode=None, stdout="", stderr="runner-1 did not answer\n")
    runner = UnansweringRunner(result=unanswered)
    service = start_service(tmp_path, runner)
    service.executions.start(TRIAGE, LEAD)
    runner.release(1)
    failed = service.wait_for_state("execution-1", "failed")
    runner.result = RunnerResult(returncode=0, stdout="done\n", stderr="")

    service.executions.start(TRIAGE, LEAD)
    runner.release(2)

    ended = service.wait_for_state("execution-2", "completed")
    assert (failed.error_output, ended.output) == ("runner-1 did not answer\n", "done\n")


def test_an_execution_whose_checkpoint_cannot_be_taken_once_the_service_starts_again_fails_saying_why(
    tmp_path: Path,
) -> None:
    first = start_service(tmp_path, HeldRunner())
    first.executions.start(TRIAGE, LEAD)
    first.wait_for_state("execution-1", "running")
    unreadable = RunnerResult(returncode=2, stdout="", stderr="tar: workspace: Permission denied\n")
    runner = FoundRunner(packed=unreadable)

    again = start_service(tmp_path, runner)

    failed = again.wait_for_state("execution-1", "failed")
    assert (failed.error_output, failed.returncode, runner.commands) == (
        "the service did not record how this run ended\ntar: workspace: Permission denied\n",
        None,
        [],
    )


def test_executions_queued_when_the_service_ended_run_in_the_order_they_were_started(tmp_path: Path) -> None:
    first = start_service(tmp_path, HeldRunner())
    for task in ("first", "second", "third"):
        first.executions.start(ExecutionRequest(agent="ticket-triager", input=task), LEAD)
    first.wait_for_state("execution-1", "running")
    runner = FoundRunner()

    again = start_service(tmp_path, runner)
    for command_number in (1, 2, 3):
        runner.release(command_number)

    again.wait_for_state("execution-1", "completed")
    assert [command[3] for command in runner.commands] == ["second", "third", "first"]


def test_a_seed_that_names_an_agent_twice_stops_the_registry_and_leaves_its_database_new(
    tmp_path: Path,
) -> None:
    seeded = load_seeded_agents()
    twice = [seeded[0], seeded[0], *seeded[1:]]
    with pytest.raises(ValueError, match=f"'{seeded[0].name}' twice"):
        AgentRegistry(twice, Database(tmp_path / "agents.db"))

    again = AgentRegistry(seeded, Database(tmp_path / "agents.db"))

    assert [agent.name for agent in again.list_agents()] == sorted(agent.name for agent in seeded)


def test_a_new_service_traces_as_its_seed_says(tmp_path: Path) -> None:
    service = start_service(tmp_path, HeldRunner())

    settings = service.executions.telemetry.settings_for("eval-summarizer")

    assert (settings.tracing, settings.sample_rate, settings.capture_content) == (True, 0.2, False)


def test_telemetry_settings_changed_before_the_service_started_again_are_kept_over_the_seed(
    tmp_path: Path,
) -> None:
    changed = TelemetryConfiguration(agents={"ticket-triager": TelemetryOverrides(tracing=False)})
    start_service(tmp_path, HeldRunner()).executions.configure_telemetry(changed)

    again = start_service(tmp_path, HeldRunner())

    assert again.executions.telemetry.model_dump(exclude_defaults=True) == {
        "agents": {"ticket-triager": {"tracing": False}},
    }


def test_a_schedule_created_before_the_service_started_again_fires_under_its_credential_beside_the_seeded_one(
    tmp_path: Path,
) -> None:
    hourly = ScheduleRequest(
        agent="ticket-triager",
        input="Triage the hour's tickets.",
        interval_seconds=3600,
    )
    identity = AgentIdentity(principal="job-9", secrets={"agents": SecretStr("schedule-secret")})
    created = start_service(tmp_path, HeldRunner()).schedules.create(hourly, LEAD, identity)
    again = start_service(tmp_path, HeldRunner())

    again.schedules.fire_due(created.created_at + timedelta(hours=1))

    listed = [
        (schedule.id, schedule.created_by, schedule.executions)
        for schedule in again.schedules.list_schedules()
    ]
    assert (again.authorized, listed) == (
        [("schedule-secret", "start_execution")],
        [("schedule-1", "batch-runner", ["execution-1"]), ("schedule-2", LEAD, ["execution-2"])],
    )


def test_an_execution_started_for_a_schedule_is_its_last_one_though_the_schedule_was_never_written(
    tmp_path: Path,
) -> None:
    first = start_service(tmp_path, HeldRunner())
    fired = ExecutionRequest(agent="eval-summarizer", input="Summarize the evaluation runs.")
    first.executions.start(
        fired,
        "batch-runner",
        "schedule-1",
    )  # the process ends before the schedule's write
    again = start_service(tmp_path, HeldRunner())
    seeded = again.schedules.list_schedules()[0]

    again.schedules.fire_due(seeded.created_at + timedelta(hours=1))

    firings = [
        (line["firing"], line["execution"]) for line in map(json.loads, again.audit_lines) if "firing" in line
    ]
    assert (seeded.executions, firings, len(again.executions.list_executions())) == (
        ["execution-1"],
        [("skipped", "execution-1")],
        1,
    )


def test_a_schedule_whose_firing_was_never_written_fires_once_for_that_time(tmp_path: Path) -> None:
    # What a process leaves that fired a schedule for a time just past and ended before it moved the time on:
    # the execution it started, ended since, and the schedule still due. No scheduler of its own runs here.
    database, runner = Database(tmp_path / "agents.db"), HeldRunner()
    registry = AgentRegistry(load_seeded_agents(), database)
    first = Executions(registry, [runner], NEVER_SECONDS, lambda _: None, database, load_seeded_telemetry())
    due_at = datetime.now(UTC) - timedelta(seconds=1)
    database.records("schedules", Schedule.model_validate_json, Schedule.model_dump_json)["schedule-1"] = (
        Schedule(
            id="schedule-1",
            agent="ticket-triager",
            input="Triage the new tickets.",
            interval_seconds=3600,
            created_by=LEAD,
            principal=None,
            created_at=due_at - timedelta(hours=1),
            next_firing_at=due_at,
        )
    )
    first.start(ExecutionRequest(agent="ticket-triager", input="Triage the new tickets."), LEAD, "schedule-1")
    runner.release(1)
    wait_until(lambda: first.get_execution("execution-1").state == "completed")

    again = start_service(tmp_path, HeldRunner())
    again.schedules.fire_due(datetime.now(UTC))
    again.schedules.fire_due(datetime.now(UTC))

    firings = [
        line["execution"] for line in map(json.loads, again.audit_lines) if line.get("firing") == "fired"
    ]
    assert (firings, len(again.executions.list_executions())) == (["execution-1"], 1)


def test_an_execution_the_service_ended_in_before_it_was_told_its_resume_is_told_it_then(
    tmp_path: Path,
) -> None:
    first = start_service(tmp_path, BlockedRunner(blocked="restore"))
    first.executions.start(TRIAGE, LEAD)
    first.wait_for_state("execution-1", "running")
    first.executions.stop("execution-1")
    first.executions.resume("execution-1", ResumeRequest(input="Triage the urgent ones first."))
    first.wait_for_state("execution-1", "running")
    runner = FoundRunner()

    again = start_service(tmp_path, runner)
    runner.release(1)

    again.wait_for_state("execution-1", "completed")
    assert runner.commands[0][-1] == "Triage the urgent ones first."


def test_a_cancelled_execution_keeps_no_checkpoint(tmp_path: Path) -> None:
    service = start_service(tmp_path, HeldRunner())
    service.executions.start(TRIAGE, LEAD)
    service.wait_for_state("execution-1", "running")
    service.executions.stop("execution-1")
    kept = Database(tmp_path / "agents.db").records("checkpoints", json.loads, json.dumps)
    assert list(kept) == ["execution-1"]

    service.executions.cancel("execution-1")

    assert list(kept) == []


class StateWriteFailsOnce(Database):
    """A database whose next write of an execution in ``state`` fails, as a full disk makes one fail."""

    state: str | None = None

    def execute(self, statement: str, *parameters: str) -> list[tuple[str, ...]]:
        failing = self.state and statement.startswith("INSERT") and parameters[0] == "executions"
        if failing and f'"state":"{self.state}"' in parameters[2]:
            self.state = None
            raise sqlite3.OperationalError("database or disk is full")
        return super().execute(statement, *parameters)


def test_a_run_whose_end_could_not_be_written_is_resumed_and_its_worker_runs_the_next(tmp_path: Path) -> None:
    database, runner = StateWriteFailsOnce(tmp_path / "agents.db"), HeldRunner()
    service = start_service(tmp_path, runner, database)
    service.executions.start(TRIAGE, LEAD)
    service.wait_for_state("execution-1", "running")
    database.state = "completed"

    runner.release(1)  # its end is not written: the worker stops it as one a process left, and resumes it
    wait_until(lambda: len(runner.commands) == 2)
    runner.release(2)
    service.wait_for_state("execution-1", "completed")
    service.executions.start(TRIAGE, LEAD)
    runner.release(3)

    service.wait_for_state("execution-2", "completed")
    assert service.execution_states() == [
        "queued",
        "running",
        "queued",
        "running",
        "completed",
        "queued",
        "running",
        "completed",
    ]


def test_an_execution_whose_start_could_not_be_written_is_taken_again(tmp_path: Path) -> None:
    database, runner = StateWriteFailsOnce(tmp_path / "agents.db"), HeldRunner()
    database.state = "running"
    service = start_service(tmp_path, runner, database)

    service.executions.start(TRIAGE, LEAD)
    runner.release(1)

    service.wait_for_state("execution-1", "completed")
    assert service.execution_states() == ["queued", "running", "completed"]
