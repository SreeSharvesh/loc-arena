from __future__ import annotations

from collections.abc import Sequence

import pytest
from inspect_ai.event import Event as InspectEvent
from inspect_ai.event import InfoEvent, ModelEvent, SpanBeginEvent, SpanEndEvent, ToolEvent
from inspect_ai.log import EvalSample
from inspect_ai.model import ChatMessageUser, GenerateConfig, ModelOutput
from inspect_ai.tool import ToolCallError
from loc_arena.logging_ import transcript_lanes
from loc_arena.logging_.transcript_lanes import WORLD, Block, build_transcript


def _info(span_id: str | None, label: str) -> InfoEvent:
    return InfoEvent(data=label, span_id=span_id)


def test_build_transcript_places_blocks_by_turn_owner_and_world_by_latest_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[InspectEvent] = [
        _info("episode:episode", "setup"),
        _info("turn:agent-main:0", "main-0"),
        _info("turn:serving-agent:0", "serve-0"),
        _info("episode:episode", "between"),
        _info("turn:agent-main:1", "main-1"),
    ]
    owners = {
        "turn:agent-main:0": ("agent-main", 0),
        "turn:serving-agent:0": ("serving-agent", 0),
        "turn:agent-main:1": ("agent-main", 1),
    }

    def fake_blocks(event: InspectEvent) -> tuple[Block, ...]:
        assert isinstance(event, InfoEvent)
        return (Block("info", str(event.data), ""),)

    def fake_order(configured: Sequence[str], seen: Sequence[str]) -> tuple[str, ...]:
        return (WORLD, *configured, *[s for s in seen if s not in configured])

    monkeypatch.setattr(transcript_lanes, "_turn_owners", lambda _events: owners)
    monkeypatch.setattr(transcript_lanes, "_blocks", fake_blocks)
    monkeypatch.setattr(transcript_lanes, "_lane_order", fake_order)
    sample = EvalSample(
        id="episode", epoch=1, input="", target="", events=events, metadata={"agents": ["agent-main"]}
    )
    transcript = build_transcript(sample)
    assert transcript.sample_id == "episode"
    assert transcript.lanes == (WORLD, "agent-main", "serving-agent")
    assert transcript.rows == (-1, 0, 1)
    titles = {key: [b.title for b in blocks] for key, blocks in transcript.cells.items()}
    assert titles == {
        (WORLD, -1): ["setup"],
        ("agent-main", 0): ["main-0"],
        ("serving-agent", 0): ["serve-0"],
        (WORLD, 0): ["between"],
        ("agent-main", 1): ["main-1"],
    }


def _spans() -> list[InspectEvent]:
    return [
        SpanBeginEvent(id="episode:episode", name="episode", type="episode"),
        SpanBeginEvent(id="agent:agent-main", parent_id="episode:episode", name="agent-main", type="agent"),
        SpanBeginEvent(id="turn:agent-main:3", parent_id="agent:agent-main", name="turn 3", type="turn"),
    ]


def test_turn_owners_maps_each_turn_span_to_its_agent_and_round() -> None:
    assert transcript_lanes._turn_owners(_spans()) == {"turn:agent-main:3": ("agent-main", 3)}


def test_a_turn_span_outside_an_agent_span_raises() -> None:
    stray = SpanBeginEvent(id="turn:x:0", parent_id="episode:episode", name="turn 0", type="turn")
    with pytest.raises(ValueError, match="not a 'turn <n>' span under an agent span"):
        transcript_lanes._turn_owners([*_spans(), stray])


def test_lane_order_is_world_then_configured_then_extra_agents_once_each() -> None:
    order = transcript_lanes._lane_order(
        ["agent-main", "serving-agent"], ["serving-agent", "child-1", "agent-main"]
    )
    assert order == (WORLD, "agent-main", "serving-agent", "child-1")


def test_blocks_dispatches_by_event_type_and_ignores_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcript_lanes, "_info_block", lambda event: Block("info", "from-info", ""))
    assert transcript_lanes._blocks(_info("episode:episode", "x")) == (Block("info", "from-info", ""),)
    assert transcript_lanes._blocks(SpanBeginEvent(id="s", name="s")) == ()


def _model(phase: str | None) -> ModelEvent:
    return ModelEvent(
        model="untrusted_agent",
        role="untrusted_agent",
        input=[ChatMessageUser(content="OBJECTIVE\n\nbrief")],
        tools=[],
        tool_choice="none",
        config=GenerateConfig(),
        output=ModelOutput.from_content(model="untrusted_agent", content='{"tool": "finish"}'),
        metadata={"identity": "batch-runner", "phase": phase, "sealed_seq": 4},
    )


def test_a_model_event_becomes_a_prompt_block_and_a_reply_block() -> None:
    assert transcript_lanes._model_blocks(_model("executing")) == (
        Block("prompt", "prompt as batch-runner (executing)", "OBJECTIVE\n\nbrief"),
        Block("reply", "reply", '{"tool": "finish"}'),
    )


def test_a_world_model_call_has_no_phase_in_its_title() -> None:
    prompt, _reply = transcript_lanes._model_blocks(_model(None))
    assert prompt.title == "prompt as batch-runner"


def test_a_tool_event_shows_arguments_as_code_and_the_result_as_body() -> None:
    event = ToolEvent(id="seq-5", function="write_file", arguments={"path": "a.py"}, result='{"ok": true}')
    assert transcript_lanes._tool_block(event) == Block(
        "tool", "write_file", '{"ok": true}', code='{\n  "path": "a.py"\n}', blocked=False
    )


def test_a_blocked_tool_event_is_marked_and_shows_the_reason() -> None:
    event = ToolEvent(
        id="seq-6",
        function="spawn_subagent",
        arguments={},
        error=ToolCallError("permission", "pre-provisioned"),
    )
    block = transcript_lanes._tool_block(event)
    assert (block.title, block.body, block.blocked) == ("spawn_subagent (blocked)", "pre-provisioned", True)


def test_an_info_event_is_titled_by_its_source_with_its_data_as_json() -> None:
    event = InfoEvent(source="message", data={"target_id": "eval-agent", "payload": {"body": "go"}})
    assert transcript_lanes._info_block(event) == Block(
        "info", "message", '{\n  "payload": {\n    "body": "go"\n  },\n  "target_id": "eval-agent"\n}'
    )


def test_a_span_nested_under_a_turn_belongs_to_that_turn() -> None:
    nested = SpanBeginEvent(id="tool:1", parent_id="turn:agent-main:3", name="tool", type="tool")
    assert transcript_lanes._turn_owners([*_spans(), nested])["tool:1"] == ("agent-main", 3)


def test_a_turn_span_with_a_non_numeric_round_raises_the_contract_error() -> None:
    bad = SpanBeginEvent(id="turn:agent-main:x", parent_id="agent:agent-main", name="turn 1a", type="turn")
    with pytest.raises(ValueError, match="'turn:agent-main:x' is not a 'turn <n>' span"):
        transcript_lanes._turn_owners([*_spans(), bad])


def test_a_cycle_of_span_parents_terminates_without_an_owner() -> None:
    a = SpanBeginEvent(id="a", parent_id="b", name="a")
    b = SpanBeginEvent(id="b", parent_id="a", name="b")
    assert "a" not in transcript_lanes._turn_owners([*_spans(), a, b])


def test_a_world_event_after_a_blockless_turn_takes_that_turns_round() -> None:
    events: list[InspectEvent] = [
        *_spans(),
        SpanEndEvent(id="turn:agent-main:3"),
        _info("episode:episode", "after"),
    ]
    sample = EvalSample(
        id="episode", epoch=1, input="", target="", events=events, metadata={"agents": ["agent-main"]}
    )
    assert list(build_transcript(sample).cells) == [(WORLD, 3)]


def test_a_failed_model_call_shows_its_error_as_the_reply() -> None:
    failed = _model("deciding").model_copy(update={"error": "rate limited"})
    _prompt, reply = transcript_lanes._model_blocks(failed)
    assert (reply.title, reply.body) == ("reply (error)", "rate limited")


def test_non_ascii_arguments_stay_readable() -> None:
    event = ToolEvent(id="seq-7", function="write_file", arguments={"text": "caf\u00e9"}, result="")
    assert "caf\u00e9" in (transcript_lanes._tool_block(event).code or "")


def test_a_blocked_tool_event_keeps_its_result_after_the_reason() -> None:
    event = ToolEvent(
        id="seq-8",
        function="spawn_subagent",
        arguments={},
        result='{"blocked": true}',
        error=ToolCallError("permission", "pre-provisioned"),
    )
    assert transcript_lanes._tool_block(event).body == 'pre-provisioned\n{"blocked": true}'
