"""The tool layer: a native tool runs on the checkout, any other over MCP, each call scoped and recorded."""

from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.forge.forge import Forge
from loc_arena.forge.world import generate_world
from loc_arena.gateway.core import ProviderError
from loc_arena.live import connect_agent_tools
from loc_arena.logging_.events import read_events
from loc_arena.scaffold.mcp_client import McpTools
from loc_arena.scaffold.tools import AgentAction
from loc_arena.task import resolve_scenario
from mcp import Client
from mcp.server import MCPServer

from tests.integration._scaffold_support import Harness


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


def test_an_open_pr_records_an_action_and_no_pr_event(harness: Harness) -> None:
    opening = AgentAction("open_pr", {"repo": "meridian-serving", "title": "perf"})

    harness.tools().execute(harness.root_context(), opening, 1)

    events = [(event.kind, event.tool) for event in read_events(harness.sealed_path)]
    assert events == [("action", "open_pr")]  # the pr event is built from the forge's record after play


def test_an_open_pr_to_a_repo_outside_the_callers_scope_is_blocked_and_logged(harness: Harness) -> None:
    unscoped = AgentAction("open_pr", {"repo": "secret-repo", "title": "x"})

    harness.tools().execute(harness.root_context(), unscoped, 1)

    actions = [event for event in read_events(harness.sealed_path) if event.kind == "action"]
    assert [event.payload["blocked"] for event in actions] == [True]


def test_a_tool_the_caller_is_not_offered_is_refused(harness: Harness) -> None:
    unoffered = AgentAction("submit_job", {})  # in agent-main's scope, on a service it is not given

    result = harness.tools().execute(harness.root_context(), unoffered, 1)

    assert result == {"error": "submit_job is not offered to you", "tool": "submit_job"}


def test_a_malformed_native_call_is_an_error_result(harness: Harness) -> None:
    no_path = AgentAction("write_file", {"content": "x"})

    result = harness.tools().execute(harness.root_context(), no_path, 1)

    assert result == {"error": "bad args for write_file: 'path'", "tool": "write_file"}


def test_a_tool_whose_service_fails_on_a_provider_error_is_an_error_result(harness: Harness) -> None:
    server = _failing_server()
    harness.clients = {"agent-main": [McpTools(lambda: Client(server))]}

    result = harness.tools().execute(harness.root_context(), AgentAction("profile", {}), 1)

    assert result == {"error": "Error executing tool profile", "tool": "profile"}
