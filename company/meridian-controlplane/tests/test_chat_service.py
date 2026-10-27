"""The chat service at its HTTP interface and its MCP tools: its rules, reads, search, seed and audit."""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any

import anyio
import pytest
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from mcp import Client
from mcp.types import CallToolResult, TextContent

from meridian_controlplane import chat as chat_package
from meridian_controlplane.chat import (
    ChatError,
    ChatSeed,
    ChatStore,
    build_chat_server,
    create_chat_api,
    open_seeded_store,
)

MEI, TOMAS, PRIYA, READER = "mei", "tomas", "priya", "reader"
IDENTITIES = {
    MEI: "mei-identity",
    TOMAS: "tomas-identity",
    PRIYA: "priya-identity",
    READER: "reader-identity",
}
RIGHTS = {MEI: ("read", "write"), TOMAS: ("read", "write"), PRIYA: ("read", "write"), READER: ("read",)}
GUESSED_IDENTITY = "guessed-identity"
STARTUP_SECONDS = 10.0
SAID = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
MANIFEST = json.loads((Path(chat_package.__file__).parent / "iam-manifest.json").read_text())
# Messages 1 and 2 are a thread in #general, 3 a thread in #evalkit, and 4 a direct message from priya to mei.
EXAMPLE_SEED = ChatSeed.model_validate(
    {
        "people": [PRIYA],
        "channels": [
            {
                "name": "general",
                "topic": "Company-wide news",
                "created_by": PRIYA,
                "created_at": SAID,
                "threads": [
                    {
                        "by": PRIYA,
                        "at": SAID.replace(hour=10),
                        "body": "Welcome to the team chat. Sprint planning moves to Thursday.",
                        "replies": [
                            {
                                "by": TOMAS,
                                "at": SAID.replace(hour=11),
                                "body": "Thanks, I'll bring the datapipe numbers.",
                            },
                        ],
                    },
                ],
            },
            {
                "name": "evalkit",
                "topic": "meridian-evalkit",
                "created_by": MEI,
                "created_at": SAID,
                "threads": [
                    {
                        "by": TOMAS,
                        "at": SAID.replace(hour=12),
                        "body": "@mei the leaderboard ties flicker again on r412.",
                    },
                ],
            },
        ],
        "direct_messages": [
            {
                "by": PRIYA,
                "to": MEI,
                "at": SAID.replace(hour=13),
                "body": "Could you take the leaderboard ticket this sprint?",
            },
        ],
    },
)


class StandInAuthority:
    """Each caller's identity and rights, checked against the service's manifest before any handler runs.

    It decides as a deployment's central authority does.
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
        """The deployment's authority checks the routes against the manifest."""


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

    def read_ids(self, caller: str, path: str, **query: object) -> list[int]:
        """The ids of the messages ``caller`` reads at ``path`` with ``query``."""
        status, read = self.call_as(caller, "GET", f"{path}?{urllib.parse.urlencode(query)}")
        assert status == HTTPStatus.OK, read
        return [message["id"] for message in read["messages"]]

    def read_rows(self) -> list[tuple[Any, ...]]:
        """Every row of the messages and the edits, as stored."""
        with sqlite3.connect(self.database) as connection:
            messages = connection.execute("SELECT * FROM messages ORDER BY id").fetchall()
            return [*messages, *connection.execute("SELECT * FROM edits ORDER BY id").fetchall()]


@pytest.fixture
def store(tmp_path: Path) -> ChatStore:
    """A chat built from ``EXAMPLE_SEED``, with mei and tomas in it besides priya."""
    return open_seeded_store(tmp_path / "chat.db", EXAMPLE_SEED, [MEI, TOMAS])


@pytest.fixture
def service(store: ChatStore, tmp_path: Path) -> Iterator[RunningService]:
    audit_lines: list[str] = []
    app = create_chat_api(store, audit_lines.append, StandInAuthority())
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    listener = socket.create_server(("127.0.0.1", 0))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + STARTUP_SECONDS
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    yield RunningService(f"http://127.0.0.1:{listener.getsockname()[1]}", tmp_path / "chat.db", audit_lines)
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
    status, _ = service.call("GET", "/channels", identity)

    assert status == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize(
    ("method", "path", "body", "allowed"),
    [
        ("GET", "/channels", None, True),
        ("GET", "/channels/general/messages", None, True),
        ("GET", "/dms", None, True),
        ("GET", "/unread", None, True),
        ("GET", "/messages?text=sprint", None, True),
        ("POST", "/channels/general/messages", {"body": "Hello."}, False),
        ("POST", "/messages/1/replies", {"body": "Hello."}, False),
        ("POST", "/dms/mei", {"body": "Hello."}, False),
        ("PATCH", "/messages/1", {"body": "Hello."}, False),
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
    status, _ = service.call_as(READER, method, path, body)

    assert status == (HTTPStatus.OK if allowed else HTTPStatus.FORBIDDEN)


@pytest.mark.parametrize(
    ("query", "read"),
    [
        ({}, [(1, 1), (5, 0)]),
        ({"thread": 1}, [(1, 1), (2, 0)]),
        ({"before": 5}, [(1, 1)]),
    ],
    ids=["its latest threads", "one thread in full", "the threads before a message"],
)
def test_reading_a_channel_gives_the_messages_its_query_selects_oldest_first_with_their_reply_counts(
    service: RunningService,
    query: dict[str, int],
    read: list[tuple[int, int]],
) -> None:
    service.call_as(MEI, "POST", "/channels/general/messages", {"body": "The agenda is in the doc."})

    _, messages = service.call_as(TOMAS, "GET", f"/channels/general/messages?{urllib.parse.urlencode(query)}")

    assert [(message["id"], message["replies"]) for message in messages["messages"]] == read


def test_a_reply_joins_the_thread_in_its_channel_written_by_its_caller(service: RunningService) -> None:
    status, reply = service.call_as(MEI, "POST", "/messages/1/replies", {"body": "Thursday works."})

    assert (status, reply["channel"], reply["thread"], reply["author"]) == (
        HTTPStatus.CREATED,
        "general",
        1,
        MEI,
    )


@pytest.mark.parametrize(
    ("method", "path", "body", "status"),
    [
        ("POST", "/channels/random/messages", {"body": "Hello."}, HTTPStatus.NOT_FOUND),
        ("POST", "/messages/2/replies", {"body": "Agreed."}, HTTPStatus.BAD_REQUEST),
        ("POST", "/messages/4/replies", {"body": "Agreed."}, HTTPStatus.BAD_REQUEST),
        ("POST", "/messages/9/replies", {"body": "Agreed."}, HTTPStatus.NOT_FOUND),
        ("GET", "/channels/general/messages?thread=3", None, HTTPStatus.NOT_FOUND),
        ("POST", "/dms/nobody", {"body": "Hello."}, HTTPStatus.BAD_REQUEST),
        ("POST", "/channels/general/messages", {"body": " "}, HTTPStatus.BAD_REQUEST),
        ("PATCH", "/messages/3", {"body": "Fixed on r413."}, HTTPStatus.FORBIDDEN),
        (
            "POST",
            "/channels/general/messages",
            {"body": "Hi", "author": PRIYA},
            HTTPStatus.UNPROCESSABLE_ENTITY,
        ),
    ],
    ids=[
        "an unknown channel",
        "a reply to a reply",
        "a reply to a direct message",
        "a reply to an unknown message",
        "a thread of another channel",
        "a direct message to someone not in the chat",
        "an empty message",
        "an edit of someone else's message",
        "an author in the body",
    ],
)
def test_a_refused_request_changes_nothing(
    service: RunningService,
    method: str,
    path: str,
    body: object,
    status: HTTPStatus,
) -> None:
    rows = service.read_rows()

    refused, _ = service.call_as(MEI, method, path, body)

    assert (refused, service.read_rows()) == (status, rows)


def test_an_edit_replaces_a_message_and_keeps_the_text_it_replaced(service: RunningService) -> None:
    _, edited = service.call_as(TOMAS, "PATCH", "/messages/3", {"body": "@mei fixed on r413."})

    assert (edited["body"], edited["edited_at"] is not None, service.read_rows()[-1][1:3]) == (
        "@mei fixed on r413.",
        True,
        (3, "@mei the leaderboard ties flicker again on r412."),
    )


@pytest.mark.parametrize(
    ("caller", "query", "read"),
    [
        (MEI, {}, [4, 5]),
        (PRIYA, {}, [4]),
        (TOMAS, {}, [5]),
        (MEI, {"member": PRIYA}, [4]),
    ],
    ids=["the recipient", "the sender", "the sender of another", "with one member"],
)
def test_a_direct_message_is_read_by_its_sender_and_recipient_alone(
    service: RunningService,
    caller: str,
    query: dict[str, str],
    read: list[int],
) -> None:
    service.call_as(TOMAS, "POST", f"/dms/{MEI}", {"body": "Pairing at two?"})

    ids = service.read_ids(caller, "/dms", **query)

    assert ids == read


def test_unread_gives_the_direct_messages_and_mentions_since_the_callers_last_unread(
    service: RunningService,
) -> None:
    service.call_as(MEI, "GET", "/unread")
    service.call_as(PRIYA, "POST", f"/dms/{MEI}", {"body": "Lunch?"})
    service.call_as(MEI, "GET", "/unread")
    service.call_as(TOMAS, "POST", f"/dms/{MEI}", {"body": "Pairing at two?"})
    service.call_as(PRIYA, "POST", "/channels/general/messages", {"body": "@mei, @tomas: the retro moved."})
    service.call_as(TOMAS, "POST", "/channels/general/messages", {"body": "@meiko welcome aboard."})
    service.call_as(MEI, "POST", "/channels/general/messages", {"body": "@mei remember the slides."})
    service.call_as(TOMAS, "POST", f"/dms/{PRIYA}", {"body": "@mei is out today."})
    service.call_as(TOMAS, "POST", "/messages/1/replies", {"body": "@mei see above."})

    ids = service.read_ids(MEI, "/unread")

    assert ids == [6, 7, 11]


def test_the_first_unread_gives_the_seeded_direct_messages_and_mentions(service: RunningService) -> None:
    ids = service.read_ids(MEI, "/unread")

    assert ids == [3, 4]


@pytest.mark.parametrize(
    ("caller", "filters", "found"),
    [
        (MEI, {"text": "LEADERBOARD"}, [4, 3]),
        (TOMAS, {"text": "leaderboard"}, [3]),
        (MEI, {"text": "sprint", "channel": "general"}, [1]),
        (MEI, {"text": "the", "author": TOMAS}, [3, 2]),
    ],
    ids=[
        "the channels and the caller's direct messages, ignoring case",
        "not someone else's direct message",
        "a channel",
        "an author",
    ],
)
def test_a_search_finds_the_messages_matching_every_filter_newest_first(
    service: RunningService,
    caller: str,
    filters: dict[str, str],
    found: list[int],
) -> None:
    ids = service.read_ids(caller, "/messages", **filters)

    assert ids == found


def test_each_request_is_an_audit_line_naming_its_caller_operation_and_arguments(
    service: RunningService,
) -> None:
    service.call_as(MEI, "POST", "/channels/general/messages", {"body": "Hello."})
    service.call_as(TOMAS, "PATCH", "/messages/1", {"body": "Mine now."})
    service.call("GET", "/channels", GUESSED_IDENTITY)

    lines = [json.loads(line) for line in service.audit_lines]

    assert [(line["caller"], line["status"], line["operation"], line["arguments"]) for line in lines] == [
        (MEI, HTTPStatus.CREATED, "post", {"channel": "general", "body": "Hello."}),
        (TOMAS, HTTPStatus.FORBIDDEN, "edit_message", {"message": 1, "body": "Mine now."}),
        (None, HTTPStatus.UNAUTHORIZED, "list_channels", {}),
    ]


def call_tool(store: ChatStore, caller: str | None, tool: str, arguments: dict[str, Any]) -> CallToolResult:
    """The result of ``caller`` calling ``tool`` on the chat's MCP server over ``store``."""

    async def call() -> CallToolResult:
        async with Client(build_chat_server(store, lambda: caller)) as client:
            return await client.call_tool(tool, arguments)

    return anyio.run(call)


def test_a_tool_posts_as_its_caller(store: ChatStore) -> None:
    posted = call_tool(store, TOMAS, "post", {"channel": "evalkit", "body": "Stable sort is merged."})

    assert ((posted.structured_content or {})["id"], (posted.structured_content or {})["author"]) == (
        5,
        TOMAS,
    )


@pytest.mark.parametrize(
    ("caller", "tool", "arguments", "reason"),
    [
        (MEI, "edit_message", {"message": 3, "body": "Fixed."}, "only its author, tomas, can edit message 3"),
        (TOMAS, "read_channel", {"channel": "random"}, "no channel #random"),
        (None, "list_channels", {}, "no identity"),
    ],
    ids=["an edit of someone else's message", "an unknown channel", "no identity"],
)
def test_a_refused_tool_call_is_a_tool_error_naming_why(
    store: ChatStore,
    caller: str | None,
    tool: str,
    arguments: dict[str, Any],
    reason: str,
) -> None:
    refused = call_tool(store, caller, tool, arguments)

    text = " ".join(block.text for block in refused.content if isinstance(block, TextContent))
    assert (refused.is_error, reason in text) == (True, True)


def test_the_tools_are_the_apis_operations_by_name(store: ChatStore) -> None:
    server = build_chat_server(store, lambda: MEI)
    api = create_chat_api(store, print, StandInAuthority())

    tools = {tool.name for tool in anyio.run(server.list_tools)}

    operations = {getattr(route, "name", "") for route in api.routes} - {"health", "describe_api", "openapi"}
    assert tools == operations


def test_a_seeded_thread_holds_its_messages_by_their_authors_at_their_times(store: ChatStore) -> None:
    thread = store.read_channel("general", thread=1)

    assert [(message.author, message.created_at) for message in thread] == [
        (PRIYA, SAID.replace(hour=10)),
        (TOMAS, SAID.replace(hour=11)),
    ]


def test_an_existing_database_is_reopened_as_it_is_not_seeded_again(tmp_path: Path) -> None:
    open_seeded_store(tmp_path / "chat.db", EXAMPLE_SEED, [MEI, TOMAS]).post(MEI, "general", "Hello.")

    reopened = open_seeded_store(tmp_path / "chat.db", EXAMPLE_SEED, [MEI, TOMAS])

    assert [message.id for message in reopened.read_channel("general")] == [1, 5]


def test_a_seed_that_fails_partway_leaves_no_store_so_the_next_start_replays_it_whole(tmp_path: Path) -> None:
    with pytest.raises(ChatError):
        open_seeded_store(tmp_path / "chat.db", EXAMPLE_SEED, [TOMAS])

    store = open_seeded_store(tmp_path / "chat.db", EXAMPLE_SEED, [MEI, TOMAS])

    assert [path.name for path in tmp_path.iterdir()] == ["chat.db"]
    assert [message.id for message in store.read_dms(MEI)] == [4]
