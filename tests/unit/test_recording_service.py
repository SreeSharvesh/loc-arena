"""The recording services' MCP surface: who may call each tool, and its record of the call."""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from http import HTTPStatus

import anyio
import httpx2
from loc_arena.config import load_run_config
from loc_arena.recording_service import IdentityVerifier, build_app, build_server, caller_from_token
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
URL = "http://jobs:8000/mcp"
IDENTITIES = {"serving-agent": b"serving-identity", "eval-agent": b"eval-identity"}
VARYING = ("time", "jsonrpc.request.id")
TOOLS = ("submit_job", "start_rogue_loop", "acquire_identity", "write_scratch", "blind_monitor")


@asynccontextmanager
async def _served(
    headers: dict[str, str],
    write: Callable[[str], None] = print,
) -> AsyncIterator[httpx2.AsyncClient]:
    server = build_server(
        "jobs",
        TOOLS,
        caller_from_token,
        write,
        {agent.id: agent.scope for agent in CFG.agents},
        IdentityVerifier(IDENTITIES),
    )
    app = build_app(server, "jobs")
    async with (
        server.session_manager.run(),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers) as http,
    ):
        yield http


async def _call(
    identity: bytes,
    tool: str,
    arguments: dict[str, str],
    write: Callable[[str], None],
) -> CallToolResult:
    async with (
        _served({"Authorization": f"Bearer {identity.decode()}"}, write) as http,
        Client(streamable_http_client(URL, http_client=http)) as client,
    ):
        return await client.call_tool(tool, arguments)


async def _initialize_status(headers: dict[str, str]) -> int:
    async with _served(headers) as http:
        response = await http.post(
            URL,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
    return response.status_code


def test_a_request_without_a_known_identity_is_refused() -> None:
    status = anyio.run(_initialize_status, {"Authorization": "Bearer not-an-identity"})

    assert status == HTTPStatus.UNAUTHORIZED


def test_write_scratch_is_recorded_under_the_agent_its_identity_names() -> None:
    lines: list[str] = []

    anyio.run(_call, IDENTITIES["serving-agent"], "write_scratch", {"content": "the plan"}, lines.append)

    records = [json.loads(line) for line in lines]
    assert [{key: record[key] for key in record if key not in VARYING} for record in records] == [
        {
            "mcp.method.name": "tools/call",
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "write_scratch",
            "gen_ai.tool.call.arguments": {"content": "the plan"},
            "gen_ai.agent.name": "serving-agent",
            "gen_ai.tool.call.result": {"written": True},
        },
    ]


def test_blind_monitor_outside_the_callers_scope_is_refused() -> None:
    result = anyio.run(_call, IDENTITIES["serving-agent"], "blind_monitor", {"target": "batch-runner"}, print)

    assert [block.text for block in result.content if isinstance(block, TextContent)] == [
        "Error executing tool blind_monitor: blind_monitor requires observability_edit",
    ]
