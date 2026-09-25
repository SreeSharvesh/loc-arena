from __future__ import annotations

import pytest
from loc_arena.logging_.agent_trace import AgentTrace


def test_constructs_with_an_injected_wall_clock() -> None:
    AgentTrace(wall_clock=lambda: 1.0)


def test_finish_on_an_empty_trace_returns_empty_collections_and_the_boundary() -> None:
    trace = AgentTrace().finish(last_sealed_seq=7)
    assert trace.turns == ()
    assert dict(trace.sealed_lane) == {}
    assert dict(trace.mirror_lane) == {}
    assert dict(trace.mirror_to_sealed) == {}
    assert trace.model_calls == ()
    assert trace.last_sealed_seq == 7


def test_finished_lane_maps_are_read_only() -> None:
    trace = AgentTrace().finish(last_sealed_seq=-1)
    with pytest.raises(TypeError):
        trace.sealed_lane[0] = None  # type: ignore[index]
