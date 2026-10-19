"""Executions: one run of a registered agent on an input, on one of the company's runners.

An execution waits until a runner is free, runs the agent's latest version there with pi, and ends completed,
failed or cancelled; the runner is reset after each one. Each change of state is a line of the audit record
naming the execution, its agent and its runner. An execution its agent's telemetry settings trace runs pi with
its trace extension, which reports each model and tool call to the company's observability store.
"""

from __future__ import annotations

import json
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

from meridian_controlplane.agents.registry import AgentName, AgentRegistry, AgentVersion
from meridian_controlplane.agents.telemetry import TelemetryConfiguration

PI_COMMAND = "pi-on-gateway"  # on every runner: pi, run once on a model through the company's gateway
RESET_AGAIN_SECONDS = 0.2  # how long a cancel waits for a run to end before it resets the runner again
CAPTURE_CONTENT_VARIABLE = (
    "MERIDIAN_CAPTURE_CONTENT"  # set for a traced execution only: pi loads the extension
)


class ExecutionState(StrEnum):
    """Where an execution is: waiting for a runner, running on one, or ended."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionRequest(BaseModel):
    """What to run: a registered agent, and the input it is given."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: AgentName
    input: str = Field(min_length=1)


class Execution(BaseModel):
    """One run of an agent's version on an input, and how it went."""

    id: str
    agent: str
    version: AgentVersion
    input: str
    started_by: str
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

    def reset(self) -> None:
        """End whatever runs on the runner and empty its workspace."""
        ...


def build_pi_command(version: AgentVersion, execution_input: str) -> list[str]:
    """pi, run once on ``version``'s model and system prompt with ``execution_input`` as its prompt."""
    return [PI_COMMAND, version.model, version.system_prompt, execution_input]


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


class Executions:
    """Every execution by id, and a worker per runner that runs the queued ones in turn."""

    def __init__(
        self,
        registry: AgentRegistry,
        runners: Sequence[Runner],
        timeout_seconds: float,
        write_audit_line: Callable[[str], None],
    ) -> None:
        """Start a worker for each of ``runners``."""
        self._registry = registry
        self._runners_by_name = {runner.name: runner for runner in runners}
        self._timeout_seconds = timeout_seconds
        self._write_audit_line = write_audit_line
        self._telemetry = TelemetryConfiguration()
        self._executions_by_id: dict[str, Execution] = {}
        self._queued_ids: queue.Queue[str] = queue.Queue()
        self._executions_lock = threading.Lock()
        # Held while a runner starts a run, or is reset: a cancel then resets only the run it cancels.
        self._runner_locks = {runner.name: threading.Lock() for runner in runners}
        self._running_ids_by_runner: dict[str, str] = {}
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
        return self._telemetry

    def configure_telemetry(self, configuration: TelemetryConfiguration) -> TelemetryConfiguration:
        """Trace the executions started from now on as ``configuration`` says; 404 for an unknown agent."""
        for agent in configuration.agents:
            self._registry.get_agent(agent)
        self._telemetry = configuration
        return configuration

    def start(self, request: ExecutionRequest, started_by: str) -> Execution:
        """Queue a run of the agent's latest version; 404 for no such agent, 409 retired, 503 no runner."""
        if not self._runners_by_name:
            raise HTTPException(HTTPStatus.SERVICE_UNAVAILABLE, "the company has no runner to run agents on")
        agent = self._registry.get_agent(request.agent)
        if agent.retired:
            raise HTTPException(HTTPStatus.CONFLICT, f"agent {request.agent!r} is retired")
        telemetry = self._telemetry.settings_for(agent.name)
        traced = telemetry.tracing and random.random() < telemetry.sample_rate  # noqa: S311
        with self._executions_lock:
            execution = self._record(
                Execution(
                    id=f"execution-{len(self._executions_by_id) + 1}",
                    agent=agent.name,
                    version=agent.latest_version,
                    input=request.input,
                    started_by=started_by,
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
            if execution.state not in {ExecutionState.QUEUED, ExecutionState.RUNNING}:
                raise HTTPException(HTTPStatus.CONFLICT, f"execution {execution_id!r} has ended")
            execution = self._record_change(
                execution,
                state=ExecutionState.CANCELLED,
                ended_at=datetime.now(UTC),
            )
        # The command may start just after a reset, so reset until the worker has seen its run end.
        while (
            execution.runner is not None and self._running_ids_by_runner.get(execution.runner) == execution_id
        ):
            with self._runner_locks[execution.runner]:
                if self._running_ids_by_runner.get(execution.runner) == execution_id:
                    self._runners_by_name[execution.runner].reset()
            time.sleep(RESET_AGAIN_SECONDS)
        return execution

    def _run_queued_executions(self, runner: Runner) -> None:
        runner_lock = self._runner_locks[runner.name]
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
            result = runner.run(
                build_pi_command(execution.version, execution.input),
                build_trace_environment(execution),
                self._timeout_seconds,
            )
            with runner_lock:
                with self._executions_lock:
                    del self._running_ids_by_runner[runner.name]
                    execution = self._executions_by_id[execution_id]
                    if execution.state is ExecutionState.RUNNING:  # not cancelled while it ran
                        self._record_change(
                            execution,
                            state=ExecutionState.COMPLETED
                            if result.returncode == 0
                            else ExecutionState.FAILED,
                            ended_at=datetime.now(UTC),
                            returncode=result.returncode,
                            output=result.stdout,
                            error_output=result.stderr,
                        )
                runner.reset()

    def _find_execution(self, execution_id: str) -> Execution:
        if execution_id not in self._executions_by_id:
            raise HTTPException(HTTPStatus.NOT_FOUND, f"no execution {execution_id!r}")
        return self._executions_by_id[execution_id]

    def _record_change(self, execution: Execution, **changes: object) -> Execution:
        return self._record(execution.model_copy(update=changes))

    def _record(self, execution: Execution) -> Execution:
        """Keep ``execution`` as it is now, and write its state on the audit record."""
        self._executions_by_id[execution.id] = execution
        self._write_audit_line(
            json.dumps(
                {
                    "wall_ts": datetime.now(UTC).isoformat(),
                    "execution": execution.id,
                    "state": execution.state,
                    "agent": execution.agent,
                    "version": execution.version.number,
                    "runner": execution.runner,
                },
            ),
        )
        return execution
