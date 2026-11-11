"""The workspace service runs an agent's bash and file tools on its own checkout, and records each call."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from loc_arena.checkouts import Checkout, LocalRunner
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.scaffold.mcp_client import over_http
from loc_arena.settings import StackSettings
from loc_arena.workspace_service import (
    WORKSPACE_TOOLS,
    build_workspace_server,
    serve_workspace,
    workspace_key,
)

TOKENS = {"agent-a": "a" * 43, "agent-b": "b" * 43}


@pytest.fixture
def records(tmp_path: Path) -> list[dict[str, Any]]:
    return []


@pytest.fixture
def url(tmp_path: Path, records: list[dict[str, Any]]) -> Iterator[str]:
    checkouts = {agent: Checkout(tmp_path / agent, LocalRunner(output_limit_bytes=None)) for agent in TOKENS}
    for checkout in checkouts.values():
        checkout.directory.mkdir()
    code = CodeServices(checkouts=checkouts, repos=[], stack=StackSettings(shell_timeout_seconds=30))
    server = build_workspace_server(code, TOKENS, lambda line: records.append(json.loads(line)))
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    with serve_workspace(server, "127.0.0.1", port):
        yield f"http://127.0.0.1:{port}/mcp"


def call(url: str, key: str, tool: str, arguments: dict[str, str]) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        async with over_http(url, key, 30) as client:
            result = await client.call_tool(tool, arguments)
            return result.structured_content or {}

    return asyncio.run(run())


def test_an_agents_command_runs_in_its_own_checkout(url: str, tmp_path: Path) -> None:
    call(url, workspace_key(TOKENS["agent-b"]), "bash", {"command": "echo made > note.txt"})

    made = {agent: (tmp_path / agent / "note.txt").exists() for agent in TOKENS}
    assert made == {"agent-a": False, "agent-b": True}


def test_a_file_an_agent_writes_is_the_file_it_reads(url: str) -> None:
    key = workspace_key(TOKENS["agent-a"])
    call(url, key, "write_file", {"path": "src/a.py", "content": "x = 1\n"})

    read = call(url, key, "read_file", {"path": "src/a.py"})

    assert read["content"] == "x = 1\n"


def test_each_call_is_recorded_with_its_agent_arguments_and_result(
    url: str,
    records: list[dict[str, Any]],
) -> None:
    call(url, workspace_key(TOKENS["agent-a"]), "bash", {"command": "echo hi"})

    record = records[-1]
    assert (
        record["gen_ai.agent.name"],
        record["gen_ai.tool.name"],
        record["gen_ai.tool.call.arguments"],
        record["gen_ai.tool.call.result"]["stdout"],
    ) == ("agent-a", "bash", {"command": "echo hi"}, "hi\n")


def test_a_caller_without_an_agents_key_is_refused(url: str, tmp_path: Path) -> None:
    with pytest.raises(BaseException):  # noqa: B017, PT011 - the client's error group around the 401
        call(url, TOKENS["agent-a"], "bash", {"command": "echo made > note.txt"})

    assert not (tmp_path / "agent-a" / "note.txt").exists()


def test_it_offers_the_native_code_tools(url: str) -> None:
    async def list_tools() -> list[str]:
        async with over_http(url, workspace_key(TOKENS["agent-a"]), 30) as client:
            return [tool.name for tool in (await client.list_tools()).tools]

    offered = asyncio.run(list_tools())

    assert sorted(offered) == sorted(WORKSPACE_TOOLS)


def test_a_service_that_cannot_start_stops_the_play(url: str) -> None:
    taken = int(url.rsplit(":", 1)[1].split("/")[0])
    server = build_workspace_server(CodeServices(checkouts={}, repos=[], stack=StackSettings()), {}, print)

    with pytest.raises(RuntimeError, match="could not start"), serve_workspace(server, "127.0.0.1", taken):
        pass
