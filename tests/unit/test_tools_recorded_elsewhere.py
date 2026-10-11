"""A tool a service records is not logged twice, but an error with no record is logged at the boundary."""

from pathlib import Path

from loc_arena.ids import PermissionScope
from loc_arena.logging_.events import AppendOnlyLog, fingerprint, read_events
from loc_arena.scaffold.bus import Recorder
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.scaffold.mcp_client import McpTools
from loc_arena.scaffold.tools import AgentAction, AgentContext, Tools
from loc_arena.settings import StackSettings
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

WRITE_SCRATCH = AgentAction("write_scratch", {"content": "a note only the sealed log may hold"})


def _scratch(*, fails: bool) -> MCPServer:
    """A scratch service whose ``write_scratch`` succeeds, or fails and so leaves no record of the attempt."""
    server = MCPServer("scratch", log_level="WARNING")

    @server.tool()
    def write_scratch(content: str) -> dict[str, bool]:
        """Write a note on the shared scratch surface."""
        if fails:
            raise ToolError("the tool server is unreachable")
        return {"written": bool(content)}

    return server


def _tools(tmp_path: Path, server: MCPServer) -> tuple[Tools, AgentContext, Path]:
    sealed_path = tmp_path / "sealed.jsonl"
    recorder = Recorder(
        "ep",
        AppendOnlyLog(sealed_path, "ep"),
        AppendOnlyLog(tmp_path / "mirror.jsonl", "ep"),
        clock=lambda: 0.0,
    )
    tools = Tools(
        recorder,
        CodeServices(checkout=tmp_path, repos=[], stack=StackSettings()),
        {"agent-main": [McpTools(lambda: Client(server))]},
        recorded_elsewhere={"write_scratch"},
    )
    ctx = AgentContext(
        uid="agent-main",
        role="orchestrator",
        branch="sprint/main",
        scope=PermissionScope(shared_surface=True),
        client=None,  # ty: ignore[invalid-argument-type] - write_scratch uses no gateway client
    )
    return tools, ctx, sealed_path


def test_an_error_from_a_recorded_elsewhere_tool_is_logged_at_the_boundary(tmp_path: Path) -> None:
    tools, ctx, sealed_path = _tools(tmp_path, _scratch(fails=True))

    tools.execute(ctx, WRITE_SCRATCH, 1)

    actions = [event for event in read_events(sealed_path) if event.kind == "action"]
    assert [(event.tool, event.result) for event in actions] == [
        (
            "write_scratch",
            {
                "error": "Error executing tool write_scratch: the tool server is unreachable",
                "tool": "write_scratch",
            },
        ),
    ]


def test_a_successful_recorded_elsewhere_tool_logs_no_action_at_the_boundary(tmp_path: Path) -> None:
    tools, ctx, sealed_path = _tools(tmp_path, _scratch(fails=False))

    tools.execute(ctx, WRITE_SCRATCH, 1)

    written = list(read_events(sealed_path)) if sealed_path.exists() else []
    assert [event for event in written if event.kind == "action"] == []


def test_a_failed_write_scratch_logged_at_the_boundary_keeps_its_note_off_the_mirror(tmp_path: Path) -> None:
    tools, ctx, _ = _tools(tmp_path, _scratch(fails=True))

    tools.execute(ctx, WRITE_SCRATCH, 1)

    mirror = (tmp_path / "mirror.jsonl").read_text()
    assert WRITE_SCRATCH.args["content"] not in mirror
    assert fingerprint(str(WRITE_SCRATCH.args["content"])) in mirror
