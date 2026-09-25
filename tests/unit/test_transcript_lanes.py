from __future__ import annotations

from collections.abc import Sequence

import pytest
from inspect_ai.event import Event as InspectEvent
from inspect_ai.event import InfoEvent, SpanBeginEvent
from inspect_ai.log import EvalSample
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
