"""The tool layer: a native tool runs on the checkout, any other over MCP; it logs what no service records."""

from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.forge.forge import Forge
from loc_arena.forge.world import generate_world
from loc_arena.gateway.core import ProviderError
from loc_arena.live import connect_agent_tools
from loc_arena.logging_.events import fingerprint, read_events
from loc_arena.scaffold.mcp_client import McpTools, over_http
from loc_arena.scaffold.tools import AgentAction
from loc_arena.task import resolve_scenario
from mcp import Client
from mcp.server import MCPServer

from tests.integration._scaffold_support import Harness

# A client of the discard port, where nothing listens.
UNREACHABLE = McpTools(lambda: over_http("http://127.0.0.1:9/mcp", "a-key", timeout_seconds=5.0))


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


@pytest.mark.parametrize(
    ("clients", "error"),
    [([], "is not offered to you"), ([UNREACHABLE], "could not be called: the tool server is unreachable")],
    ids=["not offered", "server unreachable"],
)
def test_a_call_that_never_reached_a_service_is_logged_as_an_action_with_its_error(
    harness: Harness,
    clients: list[McpTools],
    error: str,
) -> None:
    harness.clients = {"agent-main": clients}

    result = harness.tools().execute(harness.root_context(), AgentAction("open_pr", {}), 1)

    logged = [(event.kind, event.tool, event.result) for event in read_events(harness.sealed_path)]
    expected = {"error": f"open_pr {error}", "tool": "open_pr"}
    assert (result, logged) == (expected, [("action", "open_pr", expected)])


def test_a_scratch_note_the_loop_logs_reaches_the_mirror_only_as_its_fingerprint(harness: Harness) -> None:
    harness.clients = {"agent-main": [UNREACHABLE]}
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
