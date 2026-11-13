"""Executions: one run of a registered agent on an input, on one of the company's runners.

An execution waits until a runner is free, runs the agent's latest version there with pi, and ends completed,
failed or cancelled; the runner is reset after each one. Each change of state is a line of the audit record
naming the execution, its agent, its runner and whether it is traced. An execution its agent's telemetry
settings trace runs pi with its trace extension, which reports each model and tool call to the company's
observability store. A running execution can be stopped, keeping its checkpoint (what its runner held), and
resumed later on any runner.

The executions, the checkpoints and the telemetry settings are records in a database
(``meridian_common.records``), so a service that starts again on it holds them. Its queued executions are
queued again by their numbers, the order they were started in, whenever each was last queued. One recorded
as running had its worker end with the last process: its runner's worker, before any other work, ends what
runs there, takes the checkpoint and queues the execution to resume, in one write; one a caller asks to stop
meanwhile is left stopped, and one whose checkpoint cannot be taken fails and says so. A checkpoint is kept
until its restore has ended well, so an execution stopped during its restore, as by the process ending,
resumes from that checkpoint and its runner's files are not packed. A stop under way when the process ended
is not kept. A worker resets its runner before its first work, since the last process may have ended before
its own reset, and takes no work while its runner cannot be reset.
"""

from __future__ import annotations

import json
import logging
import queue
import random
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Protocol

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from meridian_common.records import Database
from meridian_controlplane.agents.authority import AgentIdentity
from meridian_controlplane.agents.registry import AgentName, AgentRegistry, AgentVersion
from meridian_controlplane.agents.telemetry import TelemetryConfiguration

logger = logging.getLogger(__name__)
PI_COMMAND = "pi-on-gateway"  # on every runner: pi, run once on a model through the company's gateway
# How long a cancel or a stop waits for a run to end before it ends it again, and a worker for its runner.
RESET_AGAIN_SECONDS = 0.2
RESUME_INPUT = "This run was stopped and has resumed. Continue the task."
RESTARTED = "the service started again while this ran\n"  # the error output of a run no worker saw end
TELEMETRY = "settings"  # the one record of the telemetry settings
CAPTURE_CONTENT_VARIABLE = (
    "MERIDIAN_CAPTURE_CONTENT"  # set for a traced execution only: pi loads the extension
)


class ExecutionState(StrEnum):
    """Where an execution is: waiting for a runner, running on one, or ended."""

    QUEUED = "queued"
    RUNNING = "running"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionRequest(BaseModel):
    """What to run: a registered agent, and the input it is given."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: AgentName
    input: str = Field(min_length=1)


class ResumeRequest(BaseModel):
    """What a stopped execution is told when it resumes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input: str = Field(default=RESUME_INPUT, min_length=1)


class Execution(BaseModel):
    """One run of an agent's version on an input, and how it went."""

    id: str
    agent: str
    version: AgentVersion
    input: str
    resume_input: str | None = None  # what its latest resume told it
    started_by: str
    schedule_id: str | None = None  # the schedule that started it, if one did
    state: ExecutionState
    traced: bool
    capture_content: bool  # whether its trace keeps prompts and replies, when it is traced
    runner: str | None = None
    queued_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    returncode: int | None = None
    output: str = ""
    error_output: str = ""


class RunnerResult(BaseModel):
    """What a command did on a runner: its exit code, none when it was killed, and its output."""

    returncode: int | None
    stdout: str
    stderr: str


class Runner(Protocol):
    """One of the company's runners, where a command runs in an empty workspace."""

    @property
    def name(self) -> str:
        """The runner, as the audit record names it."""
        ...

    def run(self, argv: list[str], environment: dict[str, str], timeout_seconds: float) -> RunnerResult:
        """Run ``argv`` with ``environment`` until it exits, is killed at ``timeout_seconds``, or is reset."""
        ...

    def end_processes(self, timeout_seconds: float) -> None:
        """End whatever runs on the runner, keeping its files."""
        ...

    def checkpoint(self, timeout_seconds: float) -> RunnerResult:
        """Pack what a reset would empty; the checkpoint is the stdout of a result that exited 0."""
        ...

    def restore(self, checkpoint: str, timeout_seconds: float) -> RunnerResult:
        """Put a checkpoint's files back on the runner."""
        ...

    def reset(self) -> None:
        """End whatever runs on the runner and empty its workspace."""
        ...


def build_pi_command(execution: Execution) -> list[str]:
    """The command running pi on the execution's version and input, and on a resume what pi is told then."""
    command = [PI_COMMAND, execution.version.model, execution.version.system_prompt, execution.input]
    return [*command, execution.resume_input] if execution.resume_input else command


def build_trace_environment(execution: Execution) -> dict[str, str]:
    """What pi's trace extension reads on the runner of a traced ``execution``; none for an untraced one."""
    if not execution.traced:
        return {}
    return {
        CAPTURE_CONTENT_VARIABLE: str(execution.capture_content).lower(),
        "MERIDIAN_EXECUTION_ID": execution.id,
        "MERIDIAN_EXECUTION_STARTED_BY": execution.started_by,
        "MERIDIAN_AGENT": execution.agent,
        "MERIDIAN_AGENT_VERSION": str(execution.version.number),
        "MERIDIAN_RUNNER": str(execution.runner),
    }


def build_identity_environment(identity: AgentIdentity | None) -> dict[str, str]:
    """What an execution's runner holds of its agent's identity: a secret per service, and its badge."""
    secrets = identity.secrets if identity else {}
    if identity is not None and identity.badge is not None:
        secrets = {**secrets, "gateway": identity.badge}  # MERIDIAN_GATEWAY_TOKEN, which pi-on-gateway reads
    return {
        f"MERIDIAN_{service.upper().replace('-', '_')}_TOKEN": secret.get_secret_value()
        for service, secret in secrets.items()
    }


class Executions:
    """Every execution by id, and a worker per runner that runs the queued ones in turn."""

    def __init__(
        self,
        registry: AgentRegistry,
        runners: Sequence[Runner],
        timeout_seconds: float,
        write_audit_line: Callable[[str], None],
        database: Database | None = None,
        telemetry: TelemetryConfiguration | None = None,
    ) -> None:
        """Hold what ``database`` holds, in memory without one, and start a worker for each of ``runners``.

        A new database starts with ``telemetry`` as its settings.
        """
        self._registry = registry
        self._runners_by_name = {runner.name: runner for runner in runners}
        self._timeout_seconds = timeout_seconds
        self._write_audit_line = write_audit_line
        self._database = database or Database()
        self._telemetry = self._database.records(
            "telemetry",
            TelemetryConfiguration.model_validate_json,
            TelemetryConfiguration.model_dump_json,
        )
        if not self._telemetry:
            self.configure_telemetry(telemetry or TelemetryConfiguration())
        self._executions_by_id = self._database.records(
            "executions",
            Execution.model_validate_json,
            Execution.model_dump_json,
        )
        self._checkpoints_by_id = self._database.records("checkpoints", json.loads, json.dumps)
        recorded = list(self._executions_by_id.values())
        # What the last process left running, by runner: its worker recovers each before other work.
        self._running_ids_by_runner = {
            execution.runner: execution.id
            for execution in recorded
            if execution.state is ExecutionState.RUNNING and execution.runner
        }
        self._stopping_ids: set[str] = set()  # the running executions a caller asked to stop
        self._queued_ids: queue.Queue[str] = queue.Queue()
        for execution in recorded:
            if execution.state is ExecutionState.QUEUED:
                self._queued_ids.put(execution.id)
        self._executions_lock = threading.Lock()
        # Held while a runner starts a run, or is reset: a cancel then resets only the run it cancels.
        self._runner_locks = {runner.name: threading.Lock() for runner in runners}
        for runner in runners:
            threading.Thread(target=self._run_queued_executions, args=(runner,), daemon=True).start()

    def list_executions(self) -> list[Execution]:
        """Every execution, oldest first."""
        with self._executions_lock:
            return list(self._executions_by_id.values())

    def get_execution(self, execution_id: str) -> Execution:
        """The execution ``execution_id``; 404 if there is none."""
        with self._executions_lock:
            return self._find_execution(execution_id)

    @property
    def telemetry(self) -> TelemetryConfiguration:
        """How executions are traced from now on."""
        return self._telemetry[TELEMETRY]

    def configure_telemetry(self, configuration: TelemetryConfiguration) -> TelemetryConfiguration:
        """Trace the executions started from now on as ``configuration`` says; 404 for an unknown agent."""
        for agent in configuration.agents:
            self._registry.get_agent(agent)
        self._telemetry[TELEMETRY] = configuration
        return configuration

    def start(self, request: ExecutionRequest, started_by: str, schedule_id: str | None = None) -> Execution:
        """Queue a run of the agent's latest version; 404 for no such agent, 409 retired, 503 no runner."""
        if not self._runners_by_name:
            raise HTTPException(HTTPStatus.SERVICE_UNAVAILABLE, "the company has no runner to run agents on")
        agent = self._registry.get_agent(request.agent)
        if agent.retired:
            raise HTTPException(HTTPStatus.CONFLICT, f"agent {request.agent!r} is retired")
        telemetry = self.telemetry.settings_for(agent.name)
        traced = telemetry.tracing and random.random() < telemetry.sample_rate
        with self._executions_lock:
            execution = self._record(
                Execution(
                    id=f"execution-{len(self._executions_by_id) + 1}",
                    agent=agent.name,
                    version=agent.latest_version,
                    input=request.input,
                    started_by=started_by,
                    schedule_id=schedule_id,
                    state=ExecutionState.QUEUED,
                    traced=traced,
                    capture_content=telemetry.capture_content,
                    queued_at=datetime.now(UTC),
                ),
            )
        self._queued_ids.put(execution.id)
        return execution

    def cancel(self, execution_id: str) -> Execution:
        """Cancel the execution, ending its run if it has one; 404 if there is none, 409 if it has ended."""
        with self._executions_lock:
            execution = self._find_execution(execution_id)
            if execution.state not in {ExecutionState.QUEUED, ExecutionState.RUNNING, ExecutionState.STOPPED}:
                raise HTTPException(HTTPStatus.CONFLICT, f"execution {execution_id!r} has ended")
            with self._database.transaction():  # its end and its checkpoint's, together
                self._checkpoints_by_id.pop(execution_id, None)
                execution = self._record_change(
                    execution,
                    state=ExecutionState.CANCELLED,
                    ended_at=datetime.now(UTC),
                )
        self._end_run(execution, lambda runner: runner.reset())
        return execution

    def stop(self, execution_id: str) -> Execution:
        """Stop the running execution, keeping its checkpoint; 404 if there is none, 409 if not running."""
        with self._executions_lock:
            execution = self._find_execution(execution_id)
            if execution.state is not ExecutionState.RUNNING:
                raise HTTPException(HTTPStatus.CONFLICT, f"execution {execution_id!r} is not running")
            self._stopping_ids.add(execution_id)
        self._end_run(execution, lambda runner: runner.end_processes(self._timeout_seconds), stopping=True)
        return self.get_execution(execution_id)

    def resume(self, execution_id: str, request: ResumeRequest) -> Execution:
        """Queue the stopped execution to go on from its checkpoint; 404 if none, 409 if not stopped."""
        with self._executions_lock:
            execution = self._find_execution(execution_id)
            if execution.state is not ExecutionState.STOPPED:
                raise HTTPException(HTTPStatus.CONFLICT, f"execution {execution_id!r} is not stopped")
            execution = self._record_change(
                execution,
                state=ExecutionState.QUEUED,
                resume_input=request.input,
            )
        self._queued_ids.put(execution_id)
        return execution

    def _end_run(
        self,
        execution: Execution,
        end: Callable[[Runner], None],
        *,
        stopping: bool = False,
    ) -> None:
        """End the execution's run with ``end`` until its worker has seen it end, since it may start after.

        A stop ends the run it was asked of alone: never a later run of the execution, once it has resumed.
        """
        name = execution.runner
        if name is None:
            return

        def is_running() -> bool:
            asked = not stopping or execution.id in self._stopping_ids
            return asked and self._running_ids_by_runner.get(name) == execution.id

        while is_running():
            with self._runner_locks[name]:
                if is_running():
                    end(self._runners_by_name[name])
            time.sleep(RESET_AGAIN_SECONDS)

    def _reset(self, runner: Runner) -> None:
        """Reset ``runner``, again until it answers: its worker takes no work on one it could not empty."""
        while True:
            try:
                return runner.reset()
            except Exception as error:  # noqa: BLE001 - whatever its client raises: the worker outlives it
                logger.warning("runner %s was not reset, trying again: %s", runner.name, error)
                time.sleep(RESET_AGAIN_SECONDS)

    def _run_queued_executions(self, runner: Runner) -> None:
        runner_lock = self._runner_locks[runner.name]
        if left_id := self._running_ids_by_runner.get(runner.name):  # by the last process, on this runner
            with runner_lock:
                runner.end_processes(self._timeout_seconds)
            left = RunnerResult(returncode=None, stdout="", stderr=RESTARTED)
            self._end_run_on(runner, left_id, left, restarted=True)
        else:
            with runner_lock:
                self._reset(runner)  # the last process may have ended before its own reset
        while True:
            execution_id = self._queued_ids.get()
            with runner_lock, self._executions_lock:
                execution = self._executions_by_id[execution_id]
                if execution.state is ExecutionState.CANCELLED:
                    continue
                execution = self._record_change(
                    execution,
                    state=ExecutionState.RUNNING,
                    runner=runner.name,
                    started_at=datetime.now(UTC),
                )
                self._running_ids_by_runner[runner.name] = execution_id
                checkpoint = self._checkpoints_by_id.get(execution_id)
            with runner_lock:  # so no stop or cancel ends the run while its files come back
                result = runner.restore(checkpoint, self._timeout_seconds) if checkpoint else None
            if result is None or result.returncode == 0:
                with self._executions_lock:  # restored: the runner holds its files now
                    self._checkpoints_by_id.pop(execution_id, None)
                identity = self._registry.get_identity(execution.agent)
                result = runner.run(
                    build_pi_command(execution),
                    {**build_trace_environment(execution), **build_identity_environment(identity)},
                    self._timeout_seconds,
                )
            self._end_run_on(runner, execution_id, result)

    def _end_run_on(
        self,
        runner: Runner,
        execution_id: str,
        result: RunnerResult,
        *,
        restarted: bool = False,
    ) -> None:
        """Record how the run on ``runner`` ended with ``result``, keeping its checkpoint if it stopped.

        A run the last process left (``restarted``) is stopped too, and queued to resume in the same write,
        unless a caller asked to stop it meanwhile.
        """
        with self._runner_locks[runner.name]:
            # A run that exited 0 beat its stop, and keeps its reply.
            stopping = (restarted or execution_id in self._stopping_ids) and result.returncode != 0
            # A checkpoint still kept was not restored whole: it stands, and the runner is not packed.
            kept = stopping and execution_id in self._checkpoints_by_id
            checkpointed = runner.checkpoint(self._timeout_seconds) if stopping and not kept else None
            with self._executions_lock:
                resumed = restarted and execution_id not in self._stopping_ids
                self._stopping_ids.discard(execution_id)
                del self._running_ids_by_runner[runner.name]
                execution = self._executions_by_id[execution_id]
                error_output = execution.error_output + result.stderr
                running = execution.state is ExecutionState.RUNNING  # not cancelled while it ran
                if running and (kept or (checkpointed is not None and checkpointed.returncode == 0)):
                    with self._database.transaction():  # the checkpoint, and the state that says it is kept
                        if checkpointed is not None:
                            self._checkpoints_by_id[execution_id] = checkpointed.stdout
                        self._record_change(
                            execution,
                            state=ExecutionState.QUEUED if resumed else ExecutionState.STOPPED,
                            # What it was last told stands: a resume the last process never gave pi.
                            resume_input=execution.resume_input or (RESUME_INPUT if resumed else None),
                            runner=None,
                            error_output=error_output,
                        )
                    if resumed:
                        self._queued_ids.put(execution_id)
                elif running:
                    with self._database.transaction():  # its end, and that of a checkpoint no restore took
                        self._checkpoints_by_id.pop(execution_id, None)
                        self._record_change(
                            execution,
                            state=ExecutionState.COMPLETED
                            if result.returncode == 0
                            else ExecutionState.FAILED,
                            ended_at=datetime.now(UTC),
                            returncode=result.returncode,
                            output=result.stdout,
                            error_output=error_output + (checkpointed.stderr if checkpointed else ""),
                        )
            self._reset(runner)

    def _find_execution(self, execution_id: str) -> Execution:
        execution = self._executions_by_id.get(execution_id)
        if execution is None:
            raise HTTPException(HTTPStatus.NOT_FOUND, f"no execution {execution_id!r}")
        return execution

    def _record_change(self, execution: Execution, **changes: object) -> Execution:
        return self._record(execution.model_copy(update=changes))

    def _record(self, execution: Execution) -> Execution:
        """Keep ``execution`` as it is now, and write its state on the audit record."""
        self._executions_by_id[execution.id] = execution
        line = json.dumps(
            {
                "wall_ts": datetime.now(UTC).isoformat(),
                "execution": execution.id,
                "state": execution.state,
                "agent": execution.agent,
                "version": execution.version.number,
                "runner": execution.runner,
                "traced": execution.traced,
            },
        )
        self._database.after_commit(lambda: self._write_audit_line(line))  # no line of a state not kept
        return execution
