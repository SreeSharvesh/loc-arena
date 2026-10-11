"""A tool a service records is not logged twice, but an error with no record is logged at the boundary."""

from pathlib import Path

from loc_arena.ids import PermissionScope
from loc_arena.logging_.events import AppendOnlyLog, read_events
from loc_arena.scaffold.bus import Recorder
from loc_arena.scaffold.tools import AgentAction, AgentContext, Services, Tools

WRITE_SCRATCH = AgentAction("write_scratch", {"content": "x"})


class _Erroring:
    """A service whose covert tool is unreachable, so it leaves no record of the attempt."""

    def run(self, tool: str, args: dict[str, object]) -> dict[str, object]:
        _ = args
        return {"error": "the tool server is unreachable", "tool": tool}


class _Ok:
    """A service whose covert tool succeeds and is recorded for the post-play builder."""

    def run(self, tool: str, args: dict[str, object]) -> dict[str, object]:
        _ = (tool, args)
        return {"written": True}


def _no_spawn(ctx: AgentContext, args: dict[str, object], turn: int) -> dict[str, object]:
    raise AssertionError("spawn not used in this test")


def _tools(tmp_path: Path, services: Services) -> tuple[Tools, AgentContext, Path]:
    sealed_path = tmp_path / "sealed.jsonl"
    recorder = Recorder(
        "ep",
        AppendOnlyLog(sealed_path, "ep"),
        AppendOnlyLog(tmp_path / "mirror.jsonl", "ep"),
        clock=lambda: 0.0,
    )
    tools = Tools(recorder, services, spawn_handler=_no_spawn, recorded_elsewhere={"write_scratch"})
    ctx = AgentContext(
        uid="agent-main",
        role="orchestrator",
        branch="sprint/main",
        scope=PermissionScope(shared_surface=True),
        client=None,  # ty: ignore[invalid-argument-type] - write_scratch uses no gateway client
    )
    return tools, ctx, sealed_path


def test_an_error_from_a_recorded_elsewhere_tool_is_logged_at_the_boundary(tmp_path: Path) -> None:
    tools, ctx, sealed_path = _tools(tmp_path, _Erroring())

    tools.execute(ctx, WRITE_SCRATCH, 1)

    actions = [event for event in read_events(sealed_path) if event.kind == "action"]
    assert [(event.tool, event.result) for event in actions] == [
        ("write_scratch", {"error": "the tool server is unreachable", "tool": "write_scratch"}),
    ]


def test_a_successful_recorded_elsewhere_tool_logs_no_action_at_the_boundary(tmp_path: Path) -> None:
    tools, ctx, sealed_path = _tools(tmp_path, _Ok())

    tools.execute(ctx, WRITE_SCRATCH, 1)

    written = list(read_events(sealed_path)) if sealed_path.exists() else []
    assert [event for event in written if event.kind == "action"] == []
