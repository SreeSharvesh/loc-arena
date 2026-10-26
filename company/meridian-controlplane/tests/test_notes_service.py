"""The notes service at its HTTP interface and its MCP tools: its bounds, its rights forwarding and audit."""

from __future__ import annotations

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

import anyio
import pytest
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from mcp import Client
from mcp.types import CallToolResult, TextContent

from meridian_controlplane import notes as notes_package
from meridian_controlplane.agents import RightsChange
from meridian_controlplane.notes import (
    NoteSeed,
    NoteStore,
    build_notes_server,
    create_notes_api,
    open_seeded_store,
)

MEI, TOMAS, READER = "mei", "tomas", "reader"
IDENTITIES = {MEI: "mei-identity", TOMAS: "tomas-identity", READER: "reader-identity"}
RIGHTS = {MEI: ["read", "write"], TOMAS: ["read", "write"], READER: ["read"]}
GUESSED_IDENTITY = "guessed-identity"
STARTUP_SECONDS = 10.0
ROTA = "## On call\n\n| week | who |\n|---|---|\n| 36 | diego |\n\nÉchange : ping #oncall 🔁"
MANIFEST = json.loads((Path(notes_package.__file__).parent / "iam-manifest.json").read_text())


class StandInAuthority:
    """Each caller's identity and rights, checked against the service's manifest before any handler runs.

    It decides as a deployment's central authority does, and keeps the rights it is asked to change; the
    engine tests the service under the real one.
    """

    def __init__(self) -> None:
        """Admit ``IDENTITIES``, each holding its ``RIGHTS``."""
        self.dependency: Callable[[Request], Awaitable[None]] = self._decide  # the protocol's attribute
        self.rights = {caller: list(rights) for caller, rights in RIGHTS.items()}
        self.changes: list[tuple[str, RightsChange, bool]] = []

    async def _decide(self, request: Request) -> None:
        """Name the operation and the caller, as a central authority does, and refuse what is not allowed."""
        operation = MANIFEST["http"][f"{request.method} {request.scope['route'].path}"]
        request.state.iam_operation = operation
        declared = MANIFEST["operations"][operation]
        if declared.get("public"):
            return
        sent = request.headers.get("Authorization", "").removeprefix("Bearer ")
        caller = next((name for name, identity in IDENTITIES.items() if identity == sent), None)
        if caller is None:
            raise HTTPException(HTTPStatus.UNAUTHORIZED, "send a known credential")
        request.state.iam_principal = caller
        if "permission" in declared and declared["permission"] not in self.rights[caller]:
            raise HTTPException(HTTPStatus.FORBIDDEN, f"needs {declared['permission']!r}")

    def verify_routes(self, app: FastAPI) -> None:
        """The deployment's authority checks the routes against the manifest; the engine tests that."""

    def authorize(self, token: str, operation: str) -> None:
        """The deployment's authority decides the service's own calls; the engine tests that."""

    async def list_rights(self, token: str) -> dict[str, list[str]]:
        """Every caller's rights."""
        _ = token
        return self.rights

    async def change_rights(self, token: str, change: RightsChange, *, granting: bool) -> list[str]:
        """Keep the change and who asked for it; the agent's rights now."""
        self.changes.append((token, change, granting))
        held = set(self.rights.get(change.agent, []))
        held = held | set(change.rights) if granting else held - set(change.rights)
        self.rights[change.agent] = sorted(held)
        return self.rights[change.agent]


@dataclass
class RunningService:
    """The service running on a local port, its authority, and the lines of its audit record so far."""

    url: str
    authority: StandInAuthority
    audit_lines: list[str] = field(default_factory=list)

    def call(
        self,
        method: str,
        path: str,
        identity: str | None,
        body: bytes | None = None,
    ) -> tuple[int, Any]:
        """The status and reply of ``method`` on ``path``, with ``identity`` sent as a bearer token."""
        headers = {
            "content-type": "application/json",
        }  # a note's text is read as it is sent, whatever its type
        if identity is not None:
            headers["authorization"] = f"Bearer {identity}"
        request = urllib.request.Request(f"{self.url}{path}", body, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=STARTUP_SECONDS) as response:
                reply = response.read().decode()
                is_json = response.headers.get_content_type() == "application/json"
                return response.status, json.loads(reply) if is_json and reply else reply
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or "null")

    def call_as(self, caller: str, method: str, path: str, body: bytes | None = None) -> tuple[int, Any]:
        """``call`` with the identity of ``caller``."""
        return self.call(method, path, IDENTITIES[caller], body)


@pytest.fixture
def store() -> NoteStore:
    """Notes holding one note, ``oncall-rota``."""
    store = NoteStore()
    store.write_note("oncall-rota", ROTA)
    return store


@pytest.fixture
def service(store: NoteStore) -> Iterator[RunningService]:
    authority, audit_lines = StandInAuthority(), []
    app = create_notes_api(store, audit_lines.append, authority)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    listener = socket.create_server(("127.0.0.1", 0))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + STARTUP_SECONDS
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    yield RunningService(f"http://127.0.0.1:{listener.getsockname()[1]}", authority, audit_lines)
    server.should_exit = True
    thread.join()


@pytest.mark.parametrize("path", ["/", "/health", "/openapi.json"])
def test_the_api_description_health_check_and_schema_need_no_identity(
    service: RunningService,
    path: str,
) -> None:
    status, _ = service.call("GET", path, identity=None)

    assert status == HTTPStatus.OK


@pytest.mark.parametrize("identity", [None, GUESSED_IDENTITY], ids=["no identity", "a guessed identity"])
def test_a_request_without_a_known_identity_is_refused(service: RunningService, identity: str | None) -> None:
    status, _ = service.call("GET", "/notes", identity)

    assert status == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize(
    ("method", "path", "body", "allowed"),
    [
        ("GET", "/notes", None, True),
        ("GET", "/notes/oncall-rota", None, True),
        ("PUT", "/notes/oncall-rota", b"mine now", False),
        ("DELETE", "/notes/oncall-rota", None, False),
    ],
)
def test_a_reader_reads_and_every_change_needs_write(
    service: RunningService,
    method: str,
    path: str,
    body: bytes | None,
    *,
    allowed: bool,
) -> None:
    status, _ = service.call_as(READER, method, path, body)

    assert status == (HTTPStatus.OK if allowed else HTTPStatus.FORBIDDEN)


def test_a_note_is_created_then_replaced_and_read_back_as_last_written(service: RunningService) -> None:
    created, _ = service.call_as(MEI, "PUT", "/notes/bench.baseline", b"cost 400")
    replaced, _ = service.call_as(TOMAS, "PUT", "/notes/bench.baseline", ROTA.encode())

    _, read = service.call_as(READER, "GET", "/notes/bench.baseline")

    _, listed = service.call_as(READER, "GET", "/notes")
    assert (created, replaced, read, listed) == (
        HTTPStatus.CREATED,
        HTTPStatus.OK,
        ROTA,
        ["bench.baseline", "oncall-rota"],
    )


def test_a_deleted_note_is_gone(service: RunningService) -> None:
    deleted, _ = service.call_as(MEI, "DELETE", "/notes/oncall-rota")

    read, _ = service.call_as(MEI, "GET", "/notes/oncall-rota")

    assert (deleted, read) == (HTTPStatus.NO_CONTENT, HTTPStatus.NOT_FOUND)


@pytest.mark.parametrize(
    ("method", "path", "body", "status"),
    [
        ("PUT", "/notes/oncall-rota", b"x" * 65_537, HTTPStatus.REQUEST_ENTITY_TOO_LARGE),
        ("PUT", "/notes/note-100", b"one too many", HTTPStatus.INSUFFICIENT_STORAGE),
        ("PUT", "/notes/a%20b", b"spaced", HTTPStatus.UNPROCESSABLE_ENTITY),
        ("PUT", "/notes/" + "k" * 65, b"long", HTTPStatus.UNPROCESSABLE_ENTITY),
        ("DELETE", "/notes/nowhere", None, HTTPStatus.NOT_FOUND),
    ],
    ids=[
        "a note past the size bound",
        "a note past the count bound",
        "a space in a key",
        "a long key",
        "no note",
    ],
)
def test_a_refused_request_changes_nothing(
    service: RunningService,
    store: NoteStore,
    method: str,
    path: str,
    body: bytes | None,
    status: HTTPStatus,
) -> None:
    for number in range(1, 100):
        store.write_note(f"note-{number}", "kept")
    before = {key: store.read_note(key) for key in store.list_notes()}

    refused, _ = service.call_as(MEI, method, path, body)

    assert (refused, {key: store.read_note(key) for key in store.list_notes()}) == (status, before)


def test_a_grant_and_a_revoke_go_to_the_authority_as_the_caller(service: RunningService) -> None:
    change = {"agent": READER, "rights": ["write"]}
    _, granted = service.call_as(MEI, "POST", "/grants", json.dumps(change).encode())
    _, revoked = service.call_as(MEI, "DELETE", "/grants", json.dumps(change).encode())

    _, listed = service.call_as(READER, "GET", "/grants")

    asked = [(token, change.agent, granting) for token, change, granting in service.authority.changes]
    assert (granted, revoked, listed[READER], asked) == (
        ["read", "write"],
        ["read"],
        ["read"],
        [(IDENTITIES[MEI], READER, True), (IDENTITIES[MEI], READER, False)],
    )


def test_each_request_is_an_audit_line_naming_its_caller_operation_and_arguments(
    service: RunningService,
) -> None:
    service.call_as(MEI, "PUT", "/notes/handover", b"Drain node-2 first.")
    service.call_as(READER, "DELETE", "/notes/handover")
    service.call("GET", "/notes", GUESSED_IDENTITY)

    lines = [json.loads(line) for line in service.audit_lines]

    assert [(line["caller"], line["status"], line["operation"], line["arguments"]) for line in lines] == [
        (MEI, HTTPStatus.CREATED, "write_note", {"key": "handover", "text": "Drain node-2 first."}),
        (READER, HTTPStatus.FORBIDDEN, "delete_note", {"key": "handover"}),
        (None, HTTPStatus.UNAUTHORIZED, "list_notes", {}),
    ]


def call_tools(
    store: NoteStore,
    caller: str | None,
    *calls: tuple[str, dict[str, Any]],
) -> list[CallToolResult]:
    """The result of each of ``calls`` that ``caller`` makes, in order, on the MCP server over ``store``."""

    async def call() -> list[CallToolResult]:
        async with Client(build_notes_server(store, lambda: caller)) as client:
            return [await client.call_tool(tool, arguments) for tool, arguments in calls]

    return anyio.run(call)


def test_a_note_a_tool_writes_is_read_back_by_a_tool(store: NoteStore) -> None:
    write = ("write_note", {"key": "handover", "text": "Drain node-2 first."})

    _, read = call_tools(store, TOMAS, write, ("read_note", {"key": "handover"}))

    assert (read.structured_content or {})["result"] == "Drain node-2 first."


@pytest.mark.parametrize(
    ("caller", "tool", "arguments", "reason"),
    [
        (TOMAS, "read_note", {"key": "nowhere"}, "no note nowhere"),
        (TOMAS, "write_note", {"key": "big", "text": "x" * 65_537}, "at most 65536 bytes"),
        (None, "list_notes", {}, "no identity"),
    ],
    ids=["an unknown note", "a note past the size bound", "no identity"],
)
def test_a_refused_tool_call_is_a_tool_error_naming_why(
    store: NoteStore,
    caller: str | None,
    tool: str,
    arguments: dict[str, Any],
    reason: str,
) -> None:
    [refused] = call_tools(store, caller, (tool, arguments))

    text = " ".join(block.text for block in refused.content if isinstance(block, TextContent))
    assert (refused.is_error, reason in text) == (True, True)


def test_the_tools_are_the_apis_note_operations_by_name(store: NoteStore) -> None:
    server = build_notes_server(store, lambda: MEI)
    api = create_notes_api(store, print, StandInAuthority())

    tools = {tool.name for tool in anyio.run(server.list_tools)}

    operations = {getattr(route, "name", "") for route in api.routes}
    rights = {"list_grants", "grant_rights", "revoke_rights"}
    assert tools == operations - {"health", "describe_api", "openapi", *rights}


def test_a_seeded_store_holds_the_seeds_notes() -> None:
    seed = NoteSeed.model_validate(
        {"notes": [{"key": "oncall-rota", "text": ROTA}, {"key": "a", "text": ""}]},
    )

    store = open_seeded_store(seed)

    assert (store.list_notes(), store.read_note("oncall-rota")) == (["a", "oncall-rota"], ROTA)
