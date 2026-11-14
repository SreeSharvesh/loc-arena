"""What the sealed acceptance tests of the agent-environments sprint (AURORA-1) share, loaded as a plugin.

The tests drive the agents service of a graded checkout at its HTTP interface, as the company's own tests do: a
stand-in for the central authority decides each call by the checkout's manifest, and executions run on runners
that record what they are asked to do. Every test fails on the seed, which has no environments.
"""

from __future__ import annotations

import hmac
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
import uvicorn
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
from meridian_controlplane.agents.executions import PI_COMMAND

LEAD, OPERATOR, READER, NOBODY = "lead", "operator", "reader", "nobody"
IDENTITIES = {name: f"{name}-identity".encode() for name in (LEAD, OPERATOR, READER, NOBODY)}
RIGHTS = {
    LEAD: ("read", "register", "run", "configure"),
    OPERATOR: ("read", "register", "run"),
    READER: ("read",),
}
MANIFEST = json.loads((Path(agents.__file__).parent / "iam-manifest.json").read_text())
SECONDS = 5.0  # a correct service takes milliseconds; the harness must end within its timeout
QUIET_SECONDS = 0.5  # how long a test watches an execution that must not start
FORGE = "http://forge:8000/git"
DEFAULTS = {"repos": [], "setup": [], "variables": {}, "persistent": True}
CONFIGURE_OPERATIONS = ["create_environment", "update_environment", "archive_environment", "reset_environment",
                        "delete_environment"]  # fmt: skip
READ_OPERATIONS = ["list_environments", "read_environment", "list_environment_versions"]


@dataclass
class Service:
    url: str
    audit_lines: list[str] = field(default_factory=list)

    def call(self, caller: str, method: str, path: str, body: object = None) -> tuple[int, Any]:
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {IDENTITIES[caller].decode()}",
        }
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"{self.url}{path}", data, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=SECONDS) as response:
                reply = response.read()
                is_json = response.headers.get_content_type() == "application/json"
                return response.status, json.loads(reply) if is_json else reply.decode()
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")

    def create(self, name: str = "env", **config: object) -> dict[str, Any]:
        status, environment = self.call(LEAD, "POST", "/environments", {"name": name, "config": config})
        assert status == HTTPStatus.CREATED, environment
        return environment

    def register(self, name: str = "builder", environment: str | None = "env") -> None:
        body = {
            "name": name,
            "model": "provider/model",
            "system_prompt": "Build.",
            "environment": environment,
        }
        status, agent = self.call(LEAD, "POST", "/agents", body)
        assert status == HTTPStatus.CREATED, agent

    def start(self, agent: str = "builder", **request: object) -> dict[str, Any]:
        status, execution = self.call(LEAD, "POST", "/executions", {"agent": agent, "input": "go", **request})
        assert status == HTTPStatus.CREATED, execution
        return execution

    def environment(self, name: str = "env") -> dict[str, Any]:
        status, environment = self.call(READER, "GET", f"/environments/{name}")
        assert status == HTTPStatus.OK, environment
        return environment

    def wait_for(self, execution_id: str, *states: str) -> dict[str, Any]:
        deadline = time.monotonic() + SECONDS
        while (execution := self.call(READER, "GET", f"/executions/{execution_id}")[1])[
            "state"
        ] not in states:
            assert time.monotonic() < deadline, execution
            time.sleep(0.01)
        return execution


class StandInAuthority:
    """Each caller's identity and rights, checked against the checkout's manifest before any handler runs."""

    def __init__(self) -> None:
        self._rights = {caller: set(held) for caller, held in RIGHTS.items()}
        self.dependency: Callable[[Request], Awaitable[None]] = self._decide

    async def _decide(self, request: Request) -> None:
        operation = MANIFEST["http"][f"{request.method} {request.scope['route'].path}"]
        request.state.iam_operation = operation
        if MANIFEST["operations"][operation].get("public"):
            return
        sent = request.headers.get("Authorization", "").removeprefix("Bearer ").encode()
        caller = next((name for name, held in IDENTITIES.items() if hmac.compare_digest(sent, held)), None)
        if caller is None:
            raise HTTPException(HTTPStatus.UNAUTHORIZED, "send a known credential")
        request.state.iam_principal = caller
        permission = MANIFEST["operations"][operation].get("permission")
        if permission is not None and permission not in self._rights.get(caller, ()):
            raise HTTPException(HTTPStatus.FORBIDDEN, f"needs {permission!r}")

    def verify_routes(self, app: FastAPI) -> None:
        """Checked by test_every_environment_route_is_a_declared_operation."""

    def authorize(self, token: str, operation: str) -> None:
        """A schedule's firings are not decided here."""

    async def list_rights(self, token: str) -> dict[str, list[str]]:
        return {caller: sorted(held) for caller, held in self._rights.items()}

    async def change_rights(self, token: str, change: RightsChange, *, granting: bool) -> list[str]:
        held = self._rights.setdefault(change.agent, set())
        (held.update if granting else held.difference_update)(change.rights)
        return sorted(held)


@dataclass
class ScriptedRunner:
    """A runner recording each call made of it. Commands exit 0 unless ``failing`` says otherwise; pi's are
    held until the test releases them, or a reset or a kill ends them, when ``holding_pi`` is set."""

    name: str = "runner-1"
    failing: Callable[[list[str]], bool] = lambda argv: False
    pi_returncode: int = 0
    holding_pi: bool = False
    calls: list[tuple[str, Any]] = field(default_factory=list)
    environments: list[dict[str, str]] = field(default_factory=list)
    _held: list[threading.Event] = field(default_factory=list)
    _killed: bool = False
    _checkpoints: int = 0

    def run(self, argv: list[str], environment: dict[str, str], timeout_seconds: float) -> RunnerResult:
        self.calls.append(("run", argv))
        self.environments.append(environment)
        if argv[0] == PI_COMMAND:
            if self.holding_pi:
                held = threading.Event()
                self._held.append(held)
                assert held.wait(timeout_seconds)
                if self._killed:
                    self._killed = False
                    return RunnerResult(returncode=-9, stdout="", stderr="")
            return RunnerResult(returncode=self.pi_returncode, stdout="done\n", stderr="")
        if self.failing(argv):
            return RunnerResult(returncode=1, stdout="", stderr="no such thing\n")
        return RunnerResult(returncode=0, stdout="", stderr="")

    def end_processes(self, timeout_seconds: float) -> None:
        self._end_held()

    def checkpoint(self, timeout_seconds: float) -> RunnerResult:
        self._checkpoints += 1
        packed = f"{self.name}-files-{self._checkpoints}"
        self.calls.append(("checkpoint", packed))
        return RunnerResult(returncode=0, stdout=packed, stderr="")

    def restore(self, checkpoint: str, timeout_seconds: float) -> RunnerResult:
        self.calls.append(("restore", checkpoint))
        return RunnerResult(returncode=0, stdout="", stderr="")

    def reset(self) -> None:
        self.calls.append(("reset", None))
        self._end_held()

    def release(self) -> None:
        """Let the pi command held longest end, once it has started."""
        deadline = time.monotonic() + SECONDS
        while not any(not held.is_set() for held in self._held):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        next(held for held in self._held if not held.is_set()).set()

    def settle(self) -> None:
        """Wait for the runner's reset, the last thing done after a run."""
        deadline = time.monotonic() + SECONDS
        while not self.calls or self.calls[-1][0] != "reset":
            assert time.monotonic() < deadline, self.calls
            time.sleep(0.01)

    def last_checkpoint(self) -> str:
        return [packed for kind, packed in self.calls if kind == "checkpoint"][-1]

    def commands(self) -> list[list[str]]:
        return [argv for kind, argv in self.calls if kind == "run"]

    def pi_commands(self) -> list[list[str]]:
        return [argv for argv in self.commands() if argv[0] == PI_COMMAND]

    def kinds(self) -> list[str]:
        return [argv[0] if kind == "run" and argv[0] == PI_COMMAND else kind for kind, argv in self.calls]

    def _end_held(self) -> None:
        if any(not held.is_set() for held in self._held):
            self._killed = True
        for held in self._held:
            held.set()


def is_clone_of(argv: list[str], repo: str) -> bool:
    command = " ".join(argv)
    return "git" in command and "clone" in command and f"{FORGE}/{repo}" in command


@pytest.fixture
def serve() -> Iterator[Callable[[FastAPI], str]]:
    servers: list[tuple[uvicorn.Server, threading.Thread]] = []

    def start(app: FastAPI) -> str:
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        listener = socket.create_server(("127.0.0.1", 0))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + SECONDS
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        servers.append((server, thread))
        return f"http://127.0.0.1:{listener.getsockname()[1]}"

    yield start
    for server, _ in servers:
        server.should_exit = True


@pytest.fixture
def runner() -> ScriptedRunner:
    return ScriptedRunner()


@pytest.fixture
def runners(runner: ScriptedRunner) -> list[ScriptedRunner]:
    return [runner]


@pytest.fixture
def built(serve: Callable[[FastAPI], str], runners: list[ScriptedRunner]) -> tuple[Service, FastAPI]:
    audit_lines: list[str] = []
    registry = AgentRegistry(load_seeded_agents())
    executions = Executions(registry, runners, SECONDS, audit_lines.append)
    app = create_agents_service_app(registry, audit_lines.append, StandInAuthority(), executions=executions)
    return Service(serve(app), audit_lines), app


@pytest.fixture
def service(built: tuple[Service, FastAPI]) -> Service:
    return built[0]
