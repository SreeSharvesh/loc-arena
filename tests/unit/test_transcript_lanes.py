from __future__ import annotations

from collections.abc import Sequence

import pytest
from inspect_ai.event import Event as InspectEvent
from inspect_ai.event import InfoEvent
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
