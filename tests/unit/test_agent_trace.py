from __future__ import annotations

from collections.abc import Callable

import pytest
from loc_arena.logging_.agent_trace import AgentTrace, TurnRecord, TurnRef


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


def _ticking_clock() -> Callable[[], float]:
    ticks = iter(range(1000))
    return lambda: float(next(ticks))


def test_turn_records_its_wall_clock_bounds() -> None:
    trace = AgentTrace(wall_clock=_ticking_clock())
    with trace.turn("agent-main", 0):
        pass
    with trace.turn("serving-agent", 0):
        pass
    assert trace.finish(last_sealed_seq=-1).turns == (
        TurnRecord(TurnRef("agent-main", 0), 0.0, 1.0),
        TurnRecord(TurnRef("serving-agent", 0), 2.0, 3.0),
    )


def test_turn_refuses_to_nest() -> None:
    trace = AgentTrace()
    with trace.turn("agent-main", 0), pytest.raises(RuntimeError, match="agent-main turn 0 is still running"):
        with trace.turn("serving-agent", 0):
            pass


def test_turn_unbinds_and_records_when_the_body_raises() -> None:
    trace = AgentTrace(wall_clock=_ticking_clock())
    with pytest.raises(ValueError), trace.turn("agent-main", 3):
        raise ValueError("tool blew up")
    with trace.turn("agent-main", 4):
        pass
    assert [t.ref.turn for t in trace.finish(last_sealed_seq=-1).turns] == [3, 4]


def test_finish_refuses_while_a_turn_is_bound() -> None:
    trace = AgentTrace()
    with trace.turn("agent-main", 0), pytest.raises(RuntimeError, match="still bound"):
        trace.finish(last_sealed_seq=-1)
