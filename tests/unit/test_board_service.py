"""The board's MCP service over Streamable HTTP: who may call it, what each agent reads, and its record."""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any

import anyio
import httpx2
import pytest
from loc_arena.board import SERVICE, Board, build_server
from loc_arena.config import load_run_config
from loc_arena.mcp_service import IdentityVerifier, build_app, caller_from_token
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
URL = "http://board:8000/mcp"
IDENTITIES = {
    "agent-main": b"main-identity",
    "serving-agent": b"serving-identity",
    "eval-agent": b"eval-identity",
}
VARYING = ("time", "jsonrpc.request.id")  # the clock, and the client's numbering of its requests
REPORT = {"to": "agent-main", "kind": "result", "body": "cost_reduction=1.5x"}
# What agent-main reads of serving-agent's report.
DELIVERED = {
    "id": 1,
    "sender": "serving-agent",
    "to": "agent-main",
    "kind": "result",
    "body": "cost_reduction=1.5x",
}

Call = tuple[str, str, dict[str, Any]]  # who calls (agent id), which tool, with what arguments


@asynccontextmanager
async def _served(
    board: Board,
    headers: dict[str, str],
    write: Callable[[str], None],
) -> AsyncIterator[httpx2.AsyncClient]:
    scopes = {agent.id: agent.scope for agent in CFG.agents}
    server = build_server(board, caller_from_token, write, scopes, IdentityVerifier(IDENTITIES))
    app = build_app(server, SERVICE)
    async with (
        server.session_manager.run(),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers) as http,
    ):
        yield http


async def _initialize_status(headers: dict[str, str]) -> int:
    async with _served(Board(), headers, print) as http:
        response = await http.post(
            URL,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
    return response.status_code


async def _call_each(calls: list[Call], write: Callable[[str], None] = print) -> list[CallToolResult]:
    """Each call on one board, each over its own connection with the caller's identity, in order."""
    board, results = Board(), []
    for agent, tool, arguments in calls:
        headers = {"Authorization": f"Bearer {IDENTITIES[agent].decode()}"}
        async with (
            _served(board, headers, write) as http,
            Client(streamable_http_client(URL, http_client=http)) as client,
        ):
            results.append(await client.call_tool(tool, arguments))
    return results


def _messages(result: CallToolResult) -> list[dict[str, Any]]:
    assert result.structured_content is not None, result.content
    return result.structured_content["messages"]


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer not-an-identity"}, {"Authorization": "main-identity"}],
    ids=["no identity", "unknown identity", "not a bearer"],
)
def test_a_request_without_a_known_identity_is_refused(headers: dict[str, str]) -> None:
    status = anyio.run(_initialize_status, headers)

    assert status == HTTPStatus.UNAUTHORIZED


def test_a_send_is_recorded_under_the_agent_its_identity_names() -> None:
    lines: list[str] = []

    anyio.run(_call_each, [("serving-agent", "send", REPORT)], lines.append)

    records = [json.loads(line) for line in lines]
    assert [{key: record[key] for key in record if key not in VARYING} for record in records] == [
        {
            "mcp.method.name": "tools/call",
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "send",
            "gen_ai.tool.call.arguments": REPORT,
            "gen_ai.agent.name": "serving-agent",
            "gen_ai.tool.call.result": {"sent": True, "id": 1, "to": "agent-main"},
        },
    ]


def test_the_recipient_reads_a_message_as_sent_by_the_agent_whose_identity_sent_it() -> None:
    spoofed = {**REPORT, "sender": "eval-agent", "actor_uid": "eval-agent"}

    results = anyio.run(_call_each, [("serving-agent", "send", spoofed), ("agent-main", "read", {})])

    assert _messages(results[-1]) == [DELIVERED]


def test_unread_returns_each_message_once() -> None:
    calls: list[Call] = [
        ("serving-agent", "send", REPORT),
        ("agent-main", "unread", {}),
        ("agent-main", "unread", {}),
    ]

    results = anyio.run(_call_each, calls)

    assert [_messages(result) for result in results[1:]] == [[DELIVERED], []]


def test_read_returns_messages_unread_has_already_returned() -> None:
    calls: list[Call] = [
        ("serving-agent", "send", REPORT),
        ("agent-main", "unread", {}),
        ("agent-main", "read", {}),
    ]

    results = anyio.run(_call_each, calls)

    assert _messages(results[-1]) == [DELIVERED]


def test_unread_returns_messages_in_the_order_they_were_sent() -> None:
    calls: list[Call] = [
        ("serving-agent", "send", {"to": "agent-main", "body": "first"}),
        ("eval-agent", "send", {"to": "agent-main", "body": "second"}),
        ("agent-main", "unread", {}),
    ]

    results = anyio.run(_call_each, calls)

    assert [(message["sender"], message["body"]) for message in _messages(results[-1])] == [
        ("serving-agent", "first"),
        ("eval-agent", "second"),
    ]


def test_a_third_agent_does_not_read_a_message_to_another() -> None:
    calls: list[Call] = [("serving-agent", "send", REPORT), ("eval-agent", "read", {})]

    results = anyio.run(_call_each, calls)

    assert _messages(results[-1]) == []


def test_a_send_outside_the_callers_scope_is_refused() -> None:
    to_a_peer = {"to": "eval-agent", "body": "launch the loop"}  # serving-agent may message agent-main alone

    results = anyio.run(_call_each, [("serving-agent", "send", to_a_peer)])

    assert [block.text for block in results[-1].content if isinstance(block, TextContent)] == [
        "Error executing tool send: send to 'eval-agent' not in allow-list ['agent-main']",
    ]
