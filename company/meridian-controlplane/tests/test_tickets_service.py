"""The tickets service at its HTTP interface and its MCP tools: its rules, search, history and audit."""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi import FastAPI, HTTPException, Request
from mcp import Client
from mcp.types import CallToolResult, TextContent

from meridian_controlplane import tickets as tickets_package
from meridian_controlplane.tickets import (
    TicketError,
    TicketSeed,
    TicketStore,
    build_tickets_server,
    create_tickets_api,
    open_seeded_store,
)

MEI, TOMAS, PRIYA, READER = "mei", "tomas", "priya", "reader"
IDENTITIES = {MEI: "mei-identity", TOMAS: "tomas-identity", READER: "reader-identity"}
RIGHTS = {MEI: ("read", "write"), TOMAS: ("read", "write"), READER: ("read",)}
GUESSED_IDENTITY = "guessed-identity"
PRINCIPALS = [MEI, TOMAS, PRIYA]
STARTUP_SECONDS = 10.0
EVALKIT = {"key": "EVALKIT", "name": "meridian-evalkit", "description": "The eval harness."}
FILED = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
MANIFEST = json.loads((Path(tickets_package.__file__).parent / "iam-manifest.json").read_text())


class StandInAuthority:
    """Each caller's identity and rights, checked against the service's manifest before any handler runs.

    It decides as a deployment's central authority does; the engine tests the service under the real one.
    """

    def __init__(self) -> None:
        """Admit ``IDENTITIES``, each holding its ``RIGHTS``."""
        self.dependency: Callable[[Request], Awaitable[None]] = self._decide  # the protocol's attribute

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
        if declared["permission"] not in RIGHTS[caller]:
            raise HTTPException(HTTPStatus.FORBIDDEN, f"needs {declared['permission']!r}")

    def verify_routes(self, app: FastAPI) -> None:
        """The deployment's authority checks the routes against the manifest; the engine tests that."""


@dataclass
class RunningService:
    """The service running on a local port, its database, and the lines of its audit record so far."""

    url: str
    database: Path
    audit_lines: list[str] = field(default_factory=list)

    def call(self, method: str, path: str, identity: str | None, body: object = None) -> tuple[int, Any]:
        """The status and reply of ``method`` on ``path``, with ``identity`` sent as a bearer token."""
        headers = {"content-type": "application/json"}
        if identity is not None:
            headers["authorization"] = f"Bearer {identity}"
        data = None if body is None else json.dumps(body).encode()
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

    def file(self, caller: str, project: str, **ticket: object) -> dict[str, Any]:
        """The ticket ``caller`` files in ``project``."""
        status, filed = self.call_as(caller, "POST", f"/projects/{project}/tickets", ticket)
        assert status == HTTPStatus.CREATED, filed
        return filed

    def search(self, **filters: str) -> list[str]:
        """The ids of the tickets a search with ``filters`` finds."""
        _, found = self.call_as(MEI, "GET", f"/tickets?{urllib.parse.urlencode(filters)}")
        return [ticket["id"] for ticket in found["tickets"]]

    def read_history(self) -> list[tuple[Any, ...]]:
        """Every history row but its time: actor, subject, action, field, old and new value, oldest first."""
        with sqlite3.connect(self.database) as connection:
            query = "SELECT actor, subject, action, field, old_value, new_value FROM history ORDER BY id"
            return connection.execute(query).fetchall()


@pytest.fixture
def store(tmp_path: Path) -> TicketStore:
    """A tracker with two projects created at ``FILED``: EVALKIT by mei and DATAPIPE by tomas."""
    store = TicketStore(tmp_path / "tickets.db", PRINCIPALS)
    store.create_project(MEI, "EVALKIT", EVALKIT["name"], EVALKIT["description"], FILED)
    store.create_project(TOMAS, "DATAPIPE", "meridian-datapipe", "", FILED)
    return store


@pytest.fixture
def service(store: TicketStore, tmp_path: Path, serve: Callable[[FastAPI], str]) -> RunningService:
    audit_lines: list[str] = []
    app = create_tickets_api(store, audit_lines.append, StandInAuthority())
    return RunningService(serve(app), tmp_path / "tickets.db", audit_lines)


@pytest.mark.parametrize("path", ["/", "/health", "/openapi.json"])
def test_the_api_description_health_check_and_schema_need_no_identity(
    service: RunningService,
    path: str,
) -> None:
    status, _ = service.call("GET", path, identity=None)

    assert status == HTTPStatus.OK


@pytest.mark.parametrize("identity", [None, GUESSED_IDENTITY], ids=["no identity", "a guessed identity"])
def test_a_request_without_a_known_identity_is_refused(service: RunningService, identity: str | None) -> None:
    status, _ = service.call("GET", "/projects", identity)

    assert status == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize(
    ("method", "path", "body", "allowed"),
    [
        ("GET", "/projects", None, True),
        ("GET", "/tickets", None, True),
        ("GET", "/tickets/EVALKIT-1", None, True),
        ("POST", "/projects", {"key": "OPS", "name": "Ops"}, False),
        ("PATCH", "/projects/EVALKIT", {"name": "Eval kit"}, False),
        ("POST", "/projects/EVALKIT/tickets", {"title": "Ties flicker"}, False),
        ("PATCH", "/tickets/EVALKIT-1", {"status": "closed"}, False),
        ("POST", "/tickets/EVALKIT-1/comments", {"body": "Seen."}, False),
    ],
)
def test_a_reader_reads_and_every_change_needs_write(
    service: RunningService,
    method: str,
    path: str,
    body: object,
    *,
    allowed: bool,
) -> None:
    service.file(MEI, "EVALKIT", title="Ties flicker")

    status, _ = service.call_as(READER, method, path, body)

    assert status == (HTTPStatus.OK if allowed else HTTPStatus.FORBIDDEN)


def test_tickets_are_numbered_per_project_and_start_open_filed_by_their_caller(
    service: RunningService,
) -> None:
    first = service.file(MEI, "EVALKIT", title="Cache teacher features")
    second = service.file(TOMAS, "EVALKIT", title="Stop re-embedding references", assignee=MEI)
    other = service.file(TOMAS, "DATAPIPE", title="Shard the holdout check")

    assert [(t["id"], t["status"], t["creator"], t["assignee"]) for t in (first, second, other)] == [
        ("EVALKIT-1", "open", MEI, None),
        ("EVALKIT-2", "open", TOMAS, MEI),
        ("DATAPIPE-1", "open", TOMAS, None),
    ]


def test_an_edit_changes_only_the_fields_it_gives_and_an_empty_string_clears_one(
    service: RunningService,
) -> None:
    filed = service.file(
        MEI,
        "EVALKIT",
        title="Cache teacher features",
        description="Per item.",
        assignee=TOMAS,
    )

    _, started = service.call_as(TOMAS, "PATCH", f"/tickets/{filed['id']}", {"status": "in_progress"})
    _, unassigned = service.call_as(TOMAS, "PATCH", f"/tickets/{filed['id']}", {"assignee": ""})

    assert (started["status"], started["assignee"], started["description"]) == (
        "in_progress",
        TOMAS,
        "Per item.",
    )
    assert (unassigned["status"], unassigned["assignee"]) == ("in_progress", None)


def test_a_sub_ticket_is_listed_on_its_parent_and_found_by_it(service: RunningService) -> None:
    parent = service.file(MEI, "EVALKIT", title="Eval harness cost")
    child = service.file(MEI, "EVALKIT", title="Cache teacher features", parent=parent["id"])

    _, read = service.call_as(TOMAS, "GET", f"/tickets/{parent['id']}")

    assert (child["parent"], read["sub_tickets"], service.search(parent=parent["id"])) == (
        "EVALKIT-1",
        ["EVALKIT-2"],
        ["EVALKIT-2"],
    )


@pytest.mark.parametrize(
    ("method", "path", "body", "status"),
    [
        ("PATCH", "/tickets/EVALKIT-2", {"assignee": "nobody"}, HTTPStatus.BAD_REQUEST),
        ("PATCH", "/tickets/EVALKIT-2", {}, HTTPStatus.BAD_REQUEST),
        ("PATCH", "/tickets/EVALKIT-2", {"status": "done"}, HTTPStatus.UNPROCESSABLE_ENTITY),
        ("PATCH", "/tickets/EVALKIT-2", {"parent": "DATAPIPE-1"}, HTTPStatus.BAD_REQUEST),
        ("PATCH", "/tickets/EVALKIT-2", {"parent": "EVALKIT-3"}, HTTPStatus.BAD_REQUEST),
        ("PATCH", "/tickets/EVALKIT-1", {"parent": "EVALKIT-4"}, HTTPStatus.BAD_REQUEST),
        ("PATCH", "/tickets/EVALKIT-2", {"title": " "}, HTTPStatus.BAD_REQUEST),
        ("PATCH", "/tickets/EVALKIT-9", {"status": "closed"}, HTTPStatus.NOT_FOUND),
        ("POST", "/projects", EVALKIT, HTTPStatus.CONFLICT),
        ("POST", "/projects", {"key": "evalkit", "name": "lower"}, HTTPStatus.UNPROCESSABLE_ENTITY),
        ("POST", "/projects/NOPE/tickets", {"title": "Lost"}, HTTPStatus.NOT_FOUND),
        (
            "POST",
            "/projects/EVALKIT/tickets",
            {"title": "Mine", "creator": PRIYA},
            HTTPStatus.UNPROCESSABLE_ENTITY,
        ),
    ],
    ids=[
        "an unknown assignee",
        "nothing to change",
        "an unknown status",
        "a parent in another project",
        "a parent that is a sub-ticket",
        "a parent for a ticket with sub-tickets",
        "an empty title",
        "an unknown ticket",
        "a taken project key",
        "a lowercase project key",
        "an unknown project",
        "a creator in the body",
    ],
)
def test_a_refused_request_changes_nothing(
    service: RunningService,
    method: str,
    path: str,
    body: object,
    status: HTTPStatus,
) -> None:
    service.file(TOMAS, "DATAPIPE", title="Shard the holdout check")
    service.file(MEI, "EVALKIT", title="Eval harness cost")
    service.file(MEI, "EVALKIT", title="Cache teacher features", parent="EVALKIT-1")
    service.file(MEI, "EVALKIT", title="Stop re-embedding references", parent="EVALKIT-1")
    service.file(MEI, "EVALKIT", title="Leaderboard ties flicker")
    history = service.read_history()

    refused, _ = service.call_as(MEI, method, path, body)

    assert (refused, service.read_history()) == (status, history)


def test_comments_are_listed_oldest_first_under_their_authors(service: RunningService) -> None:
    filed = service.file(MEI, "EVALKIT", title="Leaderboard ties flicker")
    service.call_as(TOMAS, "POST", f"/tickets/{filed['id']}/comments", {"body": "Seen on r412."})
    service.call_as(MEI, "POST", f"/tickets/{filed['id']}/comments", {"body": "Taking it."})

    _, read = service.call_as(TOMAS, "GET", f"/tickets/{filed['id']}")

    assert [(comment["author"], comment["body"]) for comment in read["comments"]] == [
        (TOMAS, "Seen on r412."),
        (MEI, "Taking it."),
    ]


@pytest.mark.parametrize(
    ("filters", "found"),
    [
        ({}, ["EVALKIT-2", "DATAPIPE-1", "EVALKIT-1"]),
        ({"project": "EVALKIT"}, ["EVALKIT-2", "EVALKIT-1"]),
        ({"status": "closed"}, ["EVALKIT-2"]),
        ({"assignee": TOMAS}, ["DATAPIPE-1"]),
        ({"creator": TOMAS, "project": "DATAPIPE"}, ["DATAPIPE-1"]),
        ({"text": "TEACHER"}, ["EVALKIT-1"]),
        ({"text": "r412"}, ["EVALKIT-2"]),
        ({"updated_since": FILED.replace(minute=30)}, ["EVALKIT-2", "DATAPIPE-1"]),
    ],
    ids=[
        "no filter, newest change (a comment) first",
        "a project",
        "a status",
        "an assignee",
        "a creator and a project",
        "text in a title, ignoring case",
        "text in a comment",
        "changed since",
    ],
)
def test_a_search_finds_the_tickets_matching_every_filter(
    store: TicketStore,
    filters: dict[str, Any],
    found: list[str],
) -> None:
    store.create_ticket(MEI, "EVALKIT", "Cache teacher features", "", None, None, FILED)
    store.create_ticket(TOMAS, "DATAPIPE", "Shard the holdout", "", TOMAS, None, FILED.replace(minute=40))
    store.create_ticket(MEI, "EVALKIT", "Ties flicker", "", None, None, FILED.replace(minute=10))
    store.edit_ticket(MEI, "EVALKIT-2", status="closed", at=FILED.replace(minute=20))
    store.comment_on_ticket(TOMAS, "EVALKIT-2", "Seen on r412.", FILED.replace(minute=50))

    tickets = store.search_tickets(**filters)

    assert [ticket.id for ticket in tickets] == found


def test_tickets_filed_at_once_each_get_their_own_number(service: RunningService) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        filed = list(pool.map(lambda n: service.file(MEI, "EVALKIT", title=f"Shard {n}")["id"], range(20)))

    assert sorted(filed) == sorted(f"EVALKIT-{n}" for n in range(1, 21))


def test_unicode_and_markdown_come_back_as_written_and_a_search_ignores_their_case(
    service: RunningService,
) -> None:
    title = "Évaluation : 同点のランキングが揺れる"
    description = "## 再現手順\n\n```python\nboard.ranking()  # 🔁 flickers\n```\n- [ ] stable sort"
    filed = service.file(MEI, "EVALKIT", title=title, description=description)

    _, read = service.call_as(TOMAS, "GET", f"/tickets/{filed['id']}")

    found = [service.search(text=text) for text in ("ÉVALUATION", "ランキング", "🔁")]
    assert (read["title"], read["description"], found) == (title, description, [[filed["id"]]] * 3)


@pytest.mark.parametrize(
    "query",
    ["status=done", "updated_since=yesterday", "updated_since=2026-03-02T09:00:00"],
    ids=["an unknown status", "a time that is no time", "a time without its offset"],
)
def test_a_search_with_a_malformed_filter_is_refused(service: RunningService, query: str) -> None:
    status, _ = service.call_as(MEI, "GET", f"/tickets?{query}")

    assert status == HTTPStatus.UNPROCESSABLE_ENTITY


def test_every_creation_change_and_comment_is_a_history_row_of_who_changed_what(
    service: RunningService,
) -> None:
    filed = service.file(MEI, "EVALKIT", title="Ties flicker", assignee=TOMAS)
    service.call_as(
        TOMAS,
        "PATCH",
        f"/tickets/{filed['id']}",
        {"status": "in_progress", "title": "Ties flicker"},
    )
    service.call_as(TOMAS, "POST", f"/tickets/{filed['id']}/comments", {"body": "Stable sort fixes it."})
    service.call_as(MEI, "PATCH", "/projects/EVALKIT", {"name": "Eval kit"})

    rows = service.read_history()

    created = {
        "title": "Ties flicker",
        "description": "",
        "status": "open",
        "assignee": TOMAS,
        "parent": None,
    }
    assert rows[2:] == [
        (MEI, "EVALKIT-1", "created", None, None, json.dumps(created)),
        (TOMAS, "EVALKIT-1", "changed", "status", "open", "in_progress"),
        (TOMAS, "EVALKIT-1", "commented", "comment", None, "Stable sort fixes it."),
        (MEI, "EVALKIT", "changed", "name", "meridian-evalkit", "Eval kit"),
    ]


def test_each_request_is_an_audit_line_naming_its_caller_operation_and_arguments(
    service: RunningService,
) -> None:
    service.call_as(MEI, "PATCH", "/projects/EVALKIT", {"name": "Eval kit"})
    service.call_as(TOMAS, "PATCH", "/tickets/EVALKIT-1", {"status": "done"})
    service.call("GET", "/projects", GUESSED_IDENTITY)

    lines = [json.loads(line) for line in service.audit_lines]

    assert [(line["caller"], line["status"], line["operation"], line["arguments"]) for line in lines] == [
        (MEI, HTTPStatus.OK, "edit_project", {"project": "EVALKIT", "name": "Eval kit"}),
        (TOMAS, HTTPStatus.UNPROCESSABLE_ENTITY, "edit_ticket", {"ticket": "EVALKIT-1"}),
        (None, HTTPStatus.UNAUTHORIZED, "list_projects", {}),
    ]


def call_tool(store: TicketStore, caller: str | None, tool: str, arguments: dict[str, Any]) -> CallToolResult:
    """The result of ``caller`` calling ``tool`` on the tracker's MCP server over ``store``."""

    async def call() -> CallToolResult:
        async with Client(build_tickets_server(store, lambda: caller)) as client:
            return await client.call_tool(tool, arguments)

    return anyio.run(call)


def test_a_tool_files_a_ticket_as_its_caller(store: TicketStore) -> None:
    filed = call_tool(store, TOMAS, "create_ticket", {"project": "EVALKIT", "title": "Ties flicker"})

    assert ((filed.structured_content or {})["id"], (filed.structured_content or {})["creator"]) == (
        "EVALKIT-1",
        TOMAS,
    )


@pytest.mark.parametrize(
    ("caller", "tool", "arguments", "reason"),
    [
        (TOMAS, "get_ticket", {"ticket": "EVALKIT-9"}, "no ticket EVALKIT-9"),
        (TOMAS, "edit_ticket", {"ticket": "EVALKIT-1", "assignee": "nobody"}, "no one named 'nobody'"),
        (None, "list_projects", {}, "no identity"),
    ],
    ids=["an unknown ticket", "an unknown assignee", "no identity"],
)
def test_a_refused_tool_call_is_a_tool_error_naming_why(
    store: TicketStore,
    caller: str | None,
    tool: str,
    arguments: dict[str, Any],
    reason: str,
) -> None:
    store.create_ticket(MEI, "EVALKIT", "Ties flicker", "", None, None)

    refused = call_tool(store, caller, tool, arguments)

    text = " ".join(block.text for block in refused.content if isinstance(block, TextContent))
    assert (refused.is_error, reason in text) == (True, True)


def test_the_tools_are_the_apis_operations_by_name(store: TicketStore) -> None:
    server = build_tickets_server(store, lambda: MEI)
    api = create_tickets_api(store, print, StandInAuthority())

    tools = {tool.name for tool in anyio.run(server.list_tools)}

    operations = {getattr(route, "name", "") for route in api.routes} - {"health", "describe_api", "openapi"}
    assert tools == operations


def test_a_seeded_ticket_keeps_its_number_its_history_and_the_next_follows_it(tmp_path: Path) -> None:
    seed = TicketSeed.model_validate(
        {
            "people": [PRIYA],
            "projects": [
                {"key": "EVALKIT", "name": "meridian-evalkit", "created_by": MEI, "created_at": FILED},
            ],
            "tickets": [
                {
                    "project": "EVALKIT",
                    "number": 58,
                    "title": "Ties flicker",
                    "creator": PRIYA,
                    "created_at": FILED,
                    "activity": [{"by": MEI, "at": FILED.replace(day=3), "assignee": TOMAS}],
                },
            ],
        },
    )
    store = open_seeded_store(tmp_path / "tickets.db", seed, [MEI, TOMAS])

    next_one = store.create_ticket(MEI, "EVALKIT", "Cache teacher features", "", PRIYA, None)

    seeded = store.get_ticket("EVALKIT-58")
    assert (seeded.assignee, seeded.updated_at, next_one.id) == (TOMAS, FILED.replace(day=3), "EVALKIT-59")


def test_an_existing_database_is_reopened_as_it_is_not_seeded_again(tmp_path: Path) -> None:
    seed = TicketSeed.model_validate(
        {
            "people": [],
            "projects": [
                {"key": "EVALKIT", "name": "meridian-evalkit", "created_by": MEI, "created_at": FILED},
            ],
            "tickets": [],
        },
    )
    open_seeded_store(tmp_path / "tickets.db", seed, [MEI]).edit_project(MEI, "EVALKIT", "Eval kit", None)

    reopened = open_seeded_store(tmp_path / "tickets.db", seed, [MEI])

    assert [project.name for project in reopened.list_projects()] == ["Eval kit"]


def test_a_seed_that_fails_partway_leaves_no_store_so_the_next_start_replays_it_whole(tmp_path: Path) -> None:
    seed = TicketSeed.model_validate(
        {
            "people": [],
            "projects": [
                {"key": "EVALKIT", "name": "meridian-evalkit", "created_by": MEI, "created_at": FILED},
            ],
            "tickets": [
                {
                    "project": "EVALKIT",
                    "title": "Ties flicker",
                    "creator": MEI,
                    "created_at": FILED,
                    "assignee": TOMAS,
                },
            ],
        },
    )
    with pytest.raises(TicketError):
        open_seeded_store(tmp_path / "tickets.db", seed, [MEI])

    store = open_seeded_store(tmp_path / "tickets.db", seed, [MEI, TOMAS])

    assert [path.name for path in tmp_path.iterdir()] == ["tickets.db"]
    assert store.get_ticket("EVALKIT-1").assignee == TOMAS
