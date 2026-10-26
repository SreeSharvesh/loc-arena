"""The agents service at its HTTP interface: who may call it, its records, and its audit record.

Its records are its registry, its executions and its telemetry settings.

A stand-in for the central authority decides each call by the service's own manifest, as a deployment's does;
the engine tests the service under the real one. Executions run on a runner that holds each command until the
test releases it or a reset or a kill ends it, so a test decides when a run ends; a stack run's real runners
are tested with the engine.
"""

from __future__ import annotations

import hmac
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request

from meridian_controlplane import agents
from meridian_controlplane.agents import (
    AgentRegistry,
    Executions,
    RightsChange,
    RunnerResult,
    create_agents_service_app,
    load_seeded_agents,
)
from meridian_controlplane.agents.executions import RESUME_INPUT

LEAD, READER, NOBODY = "lead", "reader", "nobody"
IDENTITIES = {LEAD: b"lead-identity", READER: b"reader-identity", NOBODY: b"nobody-identity"}
GUESSED_IDENTITY = b"guessed-identity"
STARTING_RIGHTS = {LEAD: ("read", "register", "run", "configure"), READER: ("read",)}
MANIFEST = json.loads((Path(agents.__file__).parent / "iam-manifest.json").read_text())
TRIAGER = {"name": "triager", "model": "provider/model", "system_prompt": "Triage the new tickets."}
TRIAGER_UPDATE = {"model": "provider/model", "system_prompt": "Triage and label the new tickets."}
SEEDED_AGENT_NAMES = ["eval-summarizer", "ticket-triager"]  # as listed in seed.yaml
STARTUP_SECONDS = 10.0
RUN_TIMEOUT_SECONDS = 10.0
SUMMARIZE = {"agent": "eval-summarizer", "input": "Summarize last night's eval run."}
KILLED = RunnerResult(returncode=-9, stdout="", stderr="")


@dataclass
class RunningService:
    """The service running on a local port, and the lines of its audit record so far."""

    url: str
    audit_lines: list[str] = field(default_factory=list)

    def call(self, method: str, path: str, identity: bytes | None, body: object = None) -> tuple[int, Any]:
        """The status and JSON reply of ``method`` on ``path``, with ``identity`` sent as a bearer token."""
        headers = {"content-type": "application/json"}
        if identity is not None:
            headers["authorization"] = f"Bearer {identity.decode()}"
        data = None if body is None else body if isinstance(body, bytes) else json.dumps(body).encode()
        request = urllib.request.Request(f"{self.url}{path}", data, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=STARTUP_SECONDS) as response:
                reply = response.read()
                is_json = response.headers.get_content_type() == "application/json"
                return response.status, json.loads(reply) if is_json else reply.decode()
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def call_as(self, caller: str, method: str, path: str, body: object = None) -> tuple[int, Any]:
        """``call`` with the identity of ``caller``."""
        return self.call(method, path, IDENTITIES[caller], body)


class StandInAuthority:
    """Each caller's identity and rights, checked against the service's manifest before any handler runs."""

    def __init__(self, identities: dict[str, bytes], rights: dict[str, tuple[str, ...]]) -> None:
        """Admit ``identities``, each holding its ``rights``."""
        self._identities = identities
        self._rights = {caller: set(held) for caller, held in rights.items()}
        self.dependency: Callable[[Request], Awaitable[None]] = self._decide  # the protocol's attribute

    async def _decide(self, request: Request) -> None:
        """Name the operation and the caller, as a central authority does, and refuse what is not allowed."""
        operation = MANIFEST["http"][f"{request.method} {request.scope['route'].path}"]
        request.state.iam_operation = operation
        permission = MANIFEST["operations"][operation].get("permission")
        if MANIFEST["operations"][operation].get("public"):
            return
        sent = request.headers.get("Authorization", "").removeprefix("Bearer ").encode()
        known = self._identities.items()
        caller = next((name for name, identity in known if hmac.compare_digest(sent, identity)), None)
        if caller is None:
            raise HTTPException(HTTPStatus.UNAUTHORIZED, "send a known credential")
        request.state.iam_principal = caller
        if permission is not None and permission not in self._rights.get(caller, ()):
            raise HTTPException(HTTPStatus.FORBIDDEN, f"needs {permission!r}")

    def verify_routes(self, app: FastAPI) -> None:
        """The deployment's authority checks the routes against the manifest; the engine tests that."""

    def authorize(self, token: str, operation: str) -> None:
        """The deployment's authority decides the service's own calls; the engine tests that."""

    async def list_rights(self, token: str) -> dict[str, list[str]]:
        """Every caller's rights."""
        return {caller: sorted(held) for caller, held in sorted(self._rights.items())}

    async def change_rights(self, token: str, change: RightsChange, *, granting: bool) -> list[str]:
        """Give or take the rights; the stand-in allows any change, deciding it is the real authority's."""
        held = self._rights.setdefault(change.agent, set())
        (held.update if granting else held.difference_update)(change.rights)
        return sorted(held)


@dataclass
class HeldRunner:
    """A runner that records each command and holds it until the test releases it, or a reset or a kill."""

    name: str = "runner-1"
    result: RunnerResult = field(
        default_factory=lambda: RunnerResult(returncode=0, stdout="done\n", stderr=""),
    )
    commands: list[list[str]] = field(default_factory=list)
    environments: list[dict[str, str]] = field(default_factory=list)
    resets: int = 0
    restored: list[str] = field(default_factory=list)
    _ends: list[threading.Event] = field(default_factory=list)
    _killed: set[int] = field(default_factory=set)

    def run(self, argv: list[str], environment: dict[str, str], timeout_seconds: float) -> RunnerResult:
        """Record ``argv`` and ``environment``, and hold the command until it is released or reset."""
        number, end = len(self._ends), threading.Event()
        self._ends.append(end)
        self.commands.append(argv)
        self.environments.append(environment)
        assert end.wait(timeout_seconds)
        return KILLED if number in self._killed else self.result

    def end_processes(self, timeout_seconds: float) -> None:
        """End what runs as a kill does: the commands held exit on the signal."""
        self._killed.update(number for number, end in enumerate(self._ends) if not end.is_set())
        for end in self._ends:
            end.set()

    def checkpoint(self, timeout_seconds: float) -> RunnerResult:
        """A checkpoint naming how many commands have run."""
        return RunnerResult(returncode=0, stdout=f"checkpoint after {len(self.commands)}", stderr="")

    def restore(self, checkpoint: str, timeout_seconds: float) -> RunnerResult:
        """Record ``checkpoint`` as put back."""
        self.restored.append(checkpoint)
        return RunnerResult(returncode=0, stdout="", stderr="")

    def reset(self) -> None:
        """End what runs, as a real runner's reset kills its command."""
        self.resets += 1
        for end in self._ends:
            end.set()

    def release(self, command_number: int) -> None:
        """Let command ``command_number``, counted from 1, end with ``result`` once it has started."""
        deadline = time.monotonic() + STARTUP_SECONDS
        while len(self._ends) < command_number:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        self._ends[command_number - 1].set()


@pytest.fixture
def authority() -> StandInAuthority:
    return StandInAuthority(IDENTITIES, STARTING_RIGHTS)


@pytest.fixture
def service(authority: StandInAuthority, serve: Callable[[FastAPI], str]) -> RunningService:
    """The service with no runner."""
    audit_lines: list[str] = []
    app = create_agents_service_app(AgentRegistry(load_seeded_agents()), audit_lines.append, authority)
    return RunningService(serve(app), audit_lines)


@pytest.fixture
def runner() -> HeldRunner:
    return HeldRunner()


@pytest.fixture
def executing_service(
    authority: StandInAuthority,
    runner: HeldRunner,
    serve: Callable[[FastAPI], str],
) -> RunningService:
    """The service with one runner, ``runner``."""
    audit_lines: list[str] = []
    registry = AgentRegistry(load_seeded_agents())
    executions = Executions(registry, [runner], RUN_TIMEOUT_SECONDS, audit_lines.append)
    app = create_agents_service_app(registry, audit_lines.append, authority, executions=executions)
    return RunningService(serve(app), audit_lines)


def wait_for_state(service: RunningService, execution_id: str, state: str) -> dict[str, Any]:
    deadline = time.monotonic() + STARTUP_SECONDS
    while (execution := service.call_as(READER, "GET", f"/executions/{execution_id}")[1])["state"] != state:
        assert time.monotonic() < deadline, execution
        time.sleep(0.01)
    return execution


def test_the_registry_starts_with_the_seeded_agents(service: RunningService) -> None:
    status, agents = service.call_as(READER, "GET", "/agents")

    assert (status, [agent["name"] for agent in agents]) == (HTTPStatus.OK, SEEDED_AGENT_NAMES)


@pytest.mark.parametrize("path", ["/", "/health", "/openapi.json"])
def test_the_api_description_health_check_and_schema_need_no_identity(
    service: RunningService,
    path: str,
) -> None:
    status, _ = service.call("GET", path, identity=None)

    assert status == HTTPStatus.OK


@pytest.mark.parametrize("identity", [None, GUESSED_IDENTITY], ids=["no identity", "a guessed identity"])
def test_a_request_without_a_known_identity_is_refused(
    service: RunningService,
    identity: bytes | None,
) -> None:
    status, _ = service.call("POST", "/agents", identity, TRIAGER)

    assert status == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize(
    ("caller", "method", "path", "body"),
    [
        (READER, "POST", "/agents", TRIAGER),
        (READER, "PUT", "/agents/ticket-triager", TRIAGER_UPDATE),
        (READER, "DELETE", "/agents/ticket-triager", None),
        (NOBODY, "GET", "/agents", None),
        (READER, "POST", "/executions", SUMMARIZE),
    ],
    ids=[
        "a reader registers",
        "a reader updates",
        "a reader retires",
        "nobody lists",
        "a reader runs an agent",
    ],
)
def test_a_caller_without_the_right_is_refused(
    service: RunningService,
    caller: str,
    method: str,
    path: str,
    body: object,
) -> None:
    status, _ = service.call_as(caller, method, path, body)

    assert status == HTTPStatus.FORBIDDEN


def test_a_registered_agent_is_owned_by_its_registrant_at_version_1(service: RunningService) -> None:
    status, agent = service.call_as(LEAD, "POST", "/agents", TRIAGER)

    version = agent["latest_version"]
    assert (status, agent["owner"], version["number"], version["created_by"]) == (
        HTTPStatus.CREATED,
        LEAD,
        1,
        LEAD,
    )


def test_an_update_makes_the_next_version_and_keeps_the_earlier_one(service: RunningService) -> None:
    service.call_as(LEAD, "POST", "/agents", TRIAGER)
    service.call_as(LEAD, "PUT", "/agents/triager", TRIAGER_UPDATE)

    _, versions = service.call_as(READER, "GET", "/agents/triager/versions")

    assert [(version["number"], version["system_prompt"]) for version in versions] == [
        (1, "Triage the new tickets."),
        (2, "Triage and label the new tickets."),
    ]


def test_a_retired_agent_is_listed_as_retired(service: RunningService) -> None:
    service.call_as(LEAD, "DELETE", "/agents/ticket-triager")

    _, agents = service.call_as(READER, "GET", "/agents")

    assert {agent["name"]: agent["retired"] for agent in agents} == {
        "eval-summarizer": False,
        "ticket-triager": True,
    }


@pytest.mark.parametrize(
    ("method", "path", "body", "expected"),
    [
        ("POST", "/agents", {**TRIAGER, "name": "ticket-triager"}, HTTPStatus.CONFLICT),
        ("POST", "/agents", {**TRIAGER, "name": "Not A Name"}, HTTPStatus.UNPROCESSABLE_ENTITY),
        ("GET", "/agents/ghost", None, HTTPStatus.NOT_FOUND),
        ("PUT", "/agents/ghost", TRIAGER_UPDATE, HTTPStatus.NOT_FOUND),
        ("PUT", "/agents/retired-triager", TRIAGER_UPDATE, HTTPStatus.CONFLICT),
        ("DELETE", "/agents/retired-triager", None, HTTPStatus.CONFLICT),
    ],
    ids=[
        "a taken name",
        "an invalid name",
        "reading no agent",
        "updating no agent",
        "updating a retired agent",
        "retiring a retired agent",
    ],
)
def test_a_change_the_registry_cannot_make_is_refused_with_its_reason(
    service: RunningService,
    method: str,
    path: str,
    body: object,
    expected: HTTPStatus,
) -> None:
    service.call_as(LEAD, "POST", "/agents", {**TRIAGER, "name": "retired-triager"})
    service.call_as(LEAD, "DELETE", "/agents/retired-triager")

    status, _ = service.call_as(LEAD, method, path, body)

    assert status == expected


def test_the_grants_routes_answer_with_the_authoritys_rights(service: RunningService) -> None:
    status, held = service.call_as(LEAD, "POST", "/grants", {"agent": READER, "rights": ["register"]})

    assert (status, held) == (HTTPStatus.OK, ["read", "register"])
    assert service.call_as(READER, "POST", "/agents", TRIAGER)[0] == HTTPStatus.CREATED


@pytest.mark.parametrize(
    ("identity", "caller", "status", "arguments"),
    [
        (IDENTITIES[LEAD], LEAD, HTTPStatus.CREATED, TRIAGER),
        (IDENTITIES[READER], READER, HTTPStatus.FORBIDDEN, {}),  # refused before the body was read
        (GUESSED_IDENTITY, None, HTTPStatus.UNAUTHORIZED, {}),
    ],
    ids=["registered", "refused", "unknown identity"],
)
def test_each_request_is_one_audit_line_naming_its_caller_operation_and_arguments(
    service: RunningService,
    identity: bytes,
    caller: str | None,
    status: HTTPStatus,
    arguments: dict[str, str],
) -> None:
    service.call("POST", "/agents", identity, TRIAGER)

    (audit_line,) = [json.loads(line) for line in service.audit_lines]
    recorded = {
        key: audit_line[key] for key in ("caller", "method", "path", "status", "operation", "arguments")
    }
    assert recorded == {
        "caller": caller,
        "method": "POST",
        "path": "/agents",
        "status": status,
        "operation": "register_agent",
        "arguments": arguments,
    }


@pytest.mark.parametrize(
    ("path", "method", "operation", "arguments"),
    [
        ("/agents", "POST", "register_agent", {}),
        ("/agents/ticket-triager", "PUT", "update_agent", {"name": "ticket-triager"}),
    ],
    ids=["registering", "updating"],
)
def test_a_malformed_body_is_refused_on_an_audit_line_naming_its_caller_and_operation(
    service: RunningService,
    path: str,
    method: str,
    operation: str,
    arguments: dict[str, str],
) -> None:
    service.call_as(LEAD, method, path, {"system_prompt": "no model"})

    (audit_line,) = [json.loads(line) for line in service.audit_lines]
    recorded = {key: audit_line[key] for key in ("caller", "status", "operation", "arguments")}
    assert recorded == {
        "caller": LEAD,
        "status": HTTPStatus.UNPROCESSABLE_ENTITY,
        "operation": operation,
        "arguments": arguments,
    }


@pytest.mark.parametrize(
    ("identity", "status", "caller"),
    [
        (IDENTITIES[LEAD], HTTPStatus.UNPROCESSABLE_ENTITY, LEAD),
        (GUESSED_IDENTITY, HTTPStatus.UNAUTHORIZED, None),
        (IDENTITIES[READER], HTTPStatus.FORBIDDEN, READER),
    ],
    ids=["a caller who may register", "an unknown identity", "a reader"],
)
def test_a_body_that_is_not_json_is_decided_by_the_authority_before_it_is_refused(
    service: RunningService,
    identity: bytes,
    status: HTTPStatus,
    caller: str | None,
) -> None:
    returned, _ = service.call("POST", "/agents", identity, b'{"name":')

    (audit_line,) = [json.loads(line) for line in service.audit_lines]
    assert (returned, audit_line["caller"], audit_line["operation"]) == (status, caller, "register_agent")


def start_summary(service: RunningService) -> dict[str, Any]:
    status, execution = service.call_as(LEAD, "POST", "/executions", SUMMARIZE)
    assert status == HTTPStatus.CREATED, execution
    return execution


def test_an_execution_runs_its_agents_latest_version_with_pi_on_a_runner(
    executing_service: RunningService,
    runner: HeldRunner,
) -> None:
    update = {"model": "provider/model", "system_prompt": "Summarize briefly."}
    executing_service.call_as(LEAD, "PUT", "/agents/eval-summarizer", update)

    execution = start_summary(executing_service)
    runner.release(1)

    ended = wait_for_state(executing_service, execution["id"], "completed")
    pi = ["pi-on-gateway", "provider/model", "Summarize briefly.", SUMMARIZE["input"]]
    assert (ended["version"]["number"], runner.commands, ended["output"]) == (2, [pi], "done\n")


@pytest.mark.parametrize(
    "result",
    [
        RunnerResult(returncode=1, stdout="", stderr="Connection error.\n"),
        RunnerResult(returncode=None, stdout="", stderr=""),
    ],
    ids=["pi exits with an error", "the runner kills it at its timeout"],
)
def test_an_execution_whose_command_does_not_exit_cleanly_fails(
    executing_service: RunningService,
    runner: HeldRunner,
    result: RunnerResult,
) -> None:
    runner.result = result

    execution = start_summary(executing_service)
    runner.release(1)

    ended = wait_for_state(executing_service, execution["id"], "failed")
    assert (ended["returncode"], ended["error_output"]) == (result.returncode, result.stderr)


def test_an_execution_waits_for_a_busy_runner_and_then_runs_on_it(
    executing_service: RunningService,
    runner: HeldRunner,
) -> None:
    first, second = start_summary(executing_service), start_summary(executing_service)
    wait_for_state(executing_service, first["id"], "running")
    _, waiting = executing_service.call_as(READER, "GET", f"/executions/{second['id']}")

    runner.release(1)

    running = wait_for_state(executing_service, second["id"], "running")
    assert (waiting["state"], running["runner"]) == ("queued", "runner-1")


def test_cancelling_a_running_execution_ends_it_and_frees_its_runner_for_the_next(
    executing_service: RunningService,
    runner: HeldRunner,
) -> None:
    first, second = start_summary(executing_service), start_summary(executing_service)
    wait_for_state(executing_service, first["id"], "running")

    status, cancelled = executing_service.call_as(LEAD, "POST", f"/executions/{first['id']}/cancel")

    wait_for_state(executing_service, second["id"], "running")
    _, first_now = executing_service.call_as(READER, "GET", f"/executions/{first['id']}")
    assert (status, cancelled["state"], first_now["state"]) == (HTTPStatus.OK, "cancelled", "cancelled")


@pytest.mark.parametrize(
    ("resume_body", "told"),
    [(None, RESUME_INPUT), ({"input": "Compare it with last week's."}, "Compare it with last week's.")],
    ids=["the-resume-message", "an-input-given"],
)
def test_a_stopped_execution_frees_its_runner_and_resumes_from_its_checkpoint(
    executing_service: RunningService,
    runner: HeldRunner,
    resume_body: dict[str, str] | None,
    told: str,
) -> None:
    stopped, other = start_summary(executing_service), start_summary(executing_service)
    wait_for_state(executing_service, stopped["id"], "running")

    status, stopping = executing_service.call_as(LEAD, "POST", f"/executions/{stopped['id']}/stop")
    runner.release(2)
    wait_for_state(executing_service, other["id"], "completed")
    executing_service.call_as(LEAD, "POST", f"/executions/{stopped['id']}/resume", resume_body)
    runner.release(3)

    wait_for_state(executing_service, stopped["id"], "completed")
    assert (status, stopping["state"], stopping["runner"]) == (HTTPStatus.OK, "stopped", None)
    assert (runner.restored, runner.commands[2]) == (["checkpoint after 1"], [*runner.commands[0], told])


@pytest.mark.parametrize(
    ("action", "state"),
    [("stop", "queued"), ("stop", "completed"), ("resume", "running"), ("resume", "completed")],
)
def test_only_a_running_execution_stops_and_only_a_stopped_one_resumes(
    executing_service: RunningService,
    runner: HeldRunner,
    action: str,
    state: str,
) -> None:
    running, queued = start_summary(executing_service), start_summary(executing_service)
    wait_for_state(executing_service, running["id"], "running")
    if state == "completed":
        runner.release(1)
        wait_for_state(executing_service, running["id"], "completed")
    target = queued if state == "queued" else running

    status, _ = executing_service.call_as(LEAD, "POST", f"/executions/{target['id']}/{action}")

    assert status == HTTPStatus.CONFLICT


def test_a_queued_execution_that_is_cancelled_never_runs(
    executing_service: RunningService,
    runner: HeldRunner,
) -> None:
    first, second = start_summary(executing_service), start_summary(executing_service)
    executing_service.call_as(LEAD, "POST", f"/executions/{second['id']}/cancel")

    runner.release(1)
    third = start_summary(executing_service)

    wait_for_state(executing_service, third["id"], "running")
    assert (len(runner.commands), first["state"]) == (2, "queued")


@pytest.mark.parametrize(
    ("agent", "refused"),
    [("retired-summarizer", HTTPStatus.CONFLICT), ("ghost", HTTPStatus.NOT_FOUND)],
    ids=["a retired agent", "no such agent"],
)
def test_an_execution_of_an_agent_that_cannot_run_is_refused(
    executing_service: RunningService,
    agent: str,
    refused: HTTPStatus,
) -> None:
    executing_service.call_as(LEAD, "POST", "/agents", {**TRIAGER, "name": "retired-summarizer"})
    executing_service.call_as(LEAD, "DELETE", "/agents/retired-summarizer")

    status, _ = executing_service.call_as(LEAD, "POST", "/executions", {"agent": agent, "input": "Go."})

    assert status == refused


def test_starting_an_execution_where_the_company_has_no_runner_is_refused(service: RunningService) -> None:
    status, _ = service.call_as(LEAD, "POST", "/executions", SUMMARIZE)

    assert status == HTTPStatus.SERVICE_UNAVAILABLE


def test_each_change_of_an_executions_state_is_an_audit_line_naming_its_runner(
    executing_service: RunningService,
    runner: HeldRunner,
) -> None:
    execution = start_summary(executing_service)
    runner.release(1)
    wait_for_state(executing_service, execution["id"], "completed")

    lines = [json.loads(line) for line in executing_service.audit_lines]
    changes = [(line["execution"], line["state"], line["runner"]) for line in lines if "execution" in line]
    assert changes == [
        ("execution-1", "queued", None),
        ("execution-1", "running", "runner-1"),
        ("execution-1", "completed", "runner-1"),
    ]


def test_telemetry_settings_are_read_with_read_and_replaced_with_configure(
    service: RunningService,
) -> None:
    settings = {
        "defaults": {"tracing": True, "sample_rate": 0.5, "capture_content": False},
        "agents": {},
    }

    refused, _ = service.call_as(READER, "PUT", "/settings/telemetry", settings)
    replaced, _ = service.call_as(LEAD, "PUT", "/settings/telemetry", settings)

    read, now = service.call_as(READER, "GET", "/settings/telemetry")
    assert (refused, replaced, read, now) == (HTTPStatus.FORBIDDEN, HTTPStatus.OK, HTTPStatus.OK, settings)


@pytest.mark.parametrize(
    ("settings", "refused"),
    [
        ({"agents": {"ghost": {"tracing": False}}}, HTTPStatus.NOT_FOUND),
        ({"defaults": {"sample_rate": 1.5}}, HTTPStatus.UNPROCESSABLE_ENTITY),
        ({"defaults": {"tracing": "sometimes"}}, HTTPStatus.UNPROCESSABLE_ENTITY),
    ],
    ids=["an unknown agent", "a sample rate over 1", "an unknown tracing setting"],
)
def test_telemetry_settings_the_service_cannot_take_are_refused(
    service: RunningService,
    settings: dict[str, Any],
    refused: HTTPStatus,
) -> None:
    status, _ = service.call_as(LEAD, "PUT", "/settings/telemetry", settings)

    assert status == refused


TRACED_WITH_FULL_CONTENT = {
    "MERIDIAN_CAPTURE_CONTENT": "true",
    "MERIDIAN_EXECUTION_ID": "execution-1",
    "MERIDIAN_EXECUTION_STARTED_BY": LEAD,
    "MERIDIAN_AGENT": "eval-summarizer",
    "MERIDIAN_AGENT_VERSION": "1",
    "MERIDIAN_RUNNER": "runner-1",
}


@pytest.mark.parametrize(
    ("settings", "environment"),
    [
        ({}, TRACED_WITH_FULL_CONTENT),
        ({"agents": {"eval-summarizer": {"tracing": False}}}, {}),
        ({"defaults": {"sample_rate": 0}}, {}),
        (
            {"defaults": {"tracing": False}, "agents": {"eval-summarizer": {"tracing": True}}},
            TRACED_WITH_FULL_CONTENT,
        ),
        (
            {"agents": {"eval-summarizer": {"capture_content": False}}},
            {**TRACED_WITH_FULL_CONTENT, "MERIDIAN_CAPTURE_CONTENT": "false"},
        ),
    ],
    ids=[
        "the defaults",
        "tracing off for its agent",
        "a sample rate of 0",
        "on for its agent alone",
        "without content",
    ],
)
def test_an_execution_is_traced_as_its_agents_settings_say_and_its_runner_is_told_so(
    executing_service: RunningService,
    runner: HeldRunner,
    settings: dict[str, Any],
    environment: dict[str, str],
) -> None:
    executing_service.call_as(LEAD, "PUT", "/settings/telemetry", settings)

    execution = start_summary(executing_service)
    runner.release(1)

    ended = wait_for_state(executing_service, execution["id"], "completed")
    captures_content = environment.get("MERIDIAN_CAPTURE_CONTENT", "true") == "true"
    assert (ended["traced"], ended["capture_content"], runner.environments) == (
        bool(environment),
        captures_content,
        [environment],
    )
