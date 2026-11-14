"""The lines a call's work writes in the call's name: as it is accepted, as it starts and as it ends.

A real MCP server with the recorder, called over its own app. The tool starts work that goes on in a thread
after the call has returned, as a deployment does.
"""

from __future__ import annotations

import contextvars
import threading

import anyio
import httpx2
from loc_arena.mcp_service import build_app
from loc_arena.tool_records import (
    ToolRecord,
    record_accepted,
    record_ended,
    record_started,
    tool_call_recorder,
)
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import MCPServer

WAIT_SECONDS = 10


def test_work_a_call_started_writes_its_lines_in_the_calls_name_once_the_call_has_returned() -> None:
    lines: list[str] = []
    returned, ended = threading.Event(), threading.Event()
    server = MCPServer("works", middleware=[tool_call_recorder(lambda: "an-agent", lines.append)])

    def work() -> None:
        returned.wait(WAIT_SECONDS)
        record_started({"release": "work-1"})
        record_ended({"number": 1, "state": "done"})
        ended.set()

    @server.tool()
    def start(what: str) -> dict[str, int]:
        record_accepted({"number": 1})
        threading.Thread(target=contextvars.copy_context().run, args=(work,), daemon=True).start()
        return {"number": 1}

    async def call() -> None:
        app = build_app(server, "works")
        async with (
            server.session_manager.run(),
            httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http,
            Client(streamable_http_client("http://works:8000/mcp", http_client=http)) as client,
        ):
            await client.call_tool("start", {"what": "a release"})

    anyio.run(call)
    returned.set()
    assert ended.wait(WAIT_SECONDS)

    accepted, called, started, done = (ToolRecord.model_validate_json(line) for line in lines)
    marks = [
        (line.accepted, line.started, line.ended, line.result) for line in (accepted, called, started, done)
    ]
    assert marks == [
        (True, False, False, {"number": 1}),
        (False, False, False, {"number": 1}),
        (False, True, False, {"release": "work-1"}),
        (False, False, True, {"number": 1, "state": "done"}),
    ]
    assert {
        (line.agent, line.tool, line.arguments["what"]) for line in (accepted, called, started, done)
    } == {
        ("an-agent", "start", "a release"),
    }
    # Each is dated as its call was made, but the last: when the work ended.
    assert (accepted.time, started.time, done.time > called.time) == (called.time, called.time, True)
