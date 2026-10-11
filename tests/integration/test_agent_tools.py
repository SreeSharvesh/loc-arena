"""The tool layer: a native tool runs on the checkout, any other over MCP; it logs what no service records."""

from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest
from loc_arena.forge.forge import Forge
from loc_arena.forge.world import generate_world
from loc_arena.gateway.core import ProviderError
from loc_arena.live import connect_agent_tools
from loc_arena.logging_.events import fingerprint, read_events
from loc_arena.scaffold.mcp_client import McpTools
from loc_arena.scaffold.tools import AgentAction
from loc_arena.task import resolve_scenario
from mcp import Client
from mcp.server import MCPServer

from tests.integration._scaffold_support import Harness, failing_tools

REFUSED = httpx2.ConnectError("connection refused")  # raised before the request is sent
LOST = httpx2.ReadTimeout("no answer in time")  # raised after the request was sent


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    """A wired episode whose agents have an in-process run's MCP clients: forge, board, covert services."""
    h = Harness(tmp_path)
    forge = Forge(generate_world(h.config, resolve_scenario(h.config), h.config.seed))
    h.clients = connect_agent_tools(forge, h.board, h.config, tmp_path)
    return h


def _failing_server() -> MCPServer:
    server = MCPServer("failing", log_level="WARNING")

    @server.tool()
    def profile() -> str:
        """Profile the pipeline."""
        raise ProviderError("the provider failed")

    return server


def test_a_call_a_service_took_is_logged_by_no_event_during_play(harness: Harness) -> None:
    opening = AgentAction("open_pr", {"repo": "meridian-serving", "title": "perf"})

    harness.tools().execute(harness.root_context(), opening, 1)

    assert not harness.sealed_path.exists()  # its events are built from the forge's record after play


@pytest.mark.parametrize("refused", [False, True], ids=["not offered", "connection refused"])
def test_a_call_that_never_reached_a_service_is_logged_as_an_action_with_its_error(
    harness: Harness,
    refused: bool,
) -> None:
    board = failing_tools(harness.connect_board("agent-main"), REFUSED, after_call=False, failures=1)
    harness.clients = {"agent-main": [board] if refused else []}

    result = harness.tools().execute(harness.root_context(), AgentAction("send", {"to": "serving-agent"}), 1)

    logged = [(event.kind, event.tool, event.result) for event in read_events(harness.sealed_path)]
    error = "could not be called: the tool server is unreachable" if refused else "is not offered to you"
    expected = {"error": f"send {error}", "tool": "send"}
    assert (result, logged) == (expected, [("action", "send", expected)])


def test_a_call_whose_answer_was_lost_is_an_unknown_outcome_left_to_the_services_record(
    harness: Harness,
) -> None:
    board = failing_tools(harness.connect_board("agent-main"), LOST, after_call=True, failures=1)
    harness.clients = {"agent-main": [board]}

    result = harness.tools().execute(harness.root_context(), AgentAction("send", {"to": "serving-agent"}), 1)

    unknown = {"error": "send was sent, but its answer was lost: the outcome is unknown", "tool": "send"}
    recorded = [
        json.loads(line)["gen_ai.tool.name"] for line in harness.board_records.read_text().splitlines()
    ]
    assert (result, harness.sealed_path.exists(), recorded) == (unknown, False, ["send"])


def test_a_scratch_note_the_loop_logs_reaches_the_mirror_only_as_its_fingerprint(harness: Harness) -> None:
    harness.clients = {"agent-main": []}
    note = "a note only the sealed log may hold"

    harness.tools().execute(harness.root_context(), AgentAction("write_scratch", {"content": note}), 1)

    mirror = harness.mirror_path.read_text()
    assert (note in mirror, fingerprint(note) in mirror) == (False, True)


def test_a_malformed_native_call_is_an_error_result(harness: Harness) -> None:
    no_path = AgentAction("write_file", {"content": "x"})

    result = harness.tools().execute(harness.root_context(), no_path, 1)

    assert result == {"error": "bad args for write_file: 'path'", "tool": "write_file"}


def test_a_tool_whose_service_fails_on_a_provider_error_is_an_error_result(harness: Harness) -> None:
    server = _failing_server()
    harness.clients = {"agent-main": [McpTools(lambda: Client(server))]}

    result = harness.tools().execute(harness.root_context(), AgentAction("profile", {}), 1)

    assert result == {"error": "Error executing tool profile", "tool": "profile"}
