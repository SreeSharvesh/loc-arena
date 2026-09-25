from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from loc_arena.logging_.agent_trace import AgentTrace, ModelCall, TurnRecord, TurnRef
from loc_arena.logging_.events import AppendOnlyLog, Event
from loc_arena.scaffold.bus import Recorder


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


def _call(trace: AgentTrace, sealed_seq: int) -> None:
    trace.on_model_call(
        identity="agent-main",
        role="untrusted_agent",
        model_input="brief",
        output="reply",
        sealed_seq=sealed_seq,
    )


def test_model_call_inside_a_turn_carries_the_turn_and_deciding_phase() -> None:
    trace = AgentTrace(wall_clock=lambda: 5.0)
    with trace.turn("agent-main", 2):
        _call(trace, 11)
    assert trace.finish(last_sealed_seq=11).model_calls == (
        ModelCall(
            TurnRef("agent-main", 2), "deciding", "agent-main", "untrusted_agent", "brief", "reply", 11, 5.0
        ),
    )


def test_model_call_outside_any_turn_has_no_turn_and_no_phase() -> None:
    trace = AgentTrace()
    _call(trace, 0)
    (call,) = trace.finish(last_sealed_seq=0).model_calls
    assert call.ref is None and call.phase is None


def test_mark_executing_switches_later_calls_in_the_turn_to_executing() -> None:
    trace = AgentTrace()
    with trace.turn("agent-main", 0):
        _call(trace, 0)
        trace.mark_executing()
        _call(trace, 1)
    with trace.turn("agent-main", 1):
        _call(trace, 2)
    phases = [c.phase for c in trace.finish(last_sealed_seq=2).model_calls]
    assert phases == ["deciding", "executing", "deciding"]


def test_mark_executing_outside_a_turn_raises() -> None:
    with pytest.raises(RuntimeError, match="no turn bound"):
        AgentTrace().mark_executing()


def _event(seq: int, **fields: object) -> Event:
    base: dict[str, object] = {
        "episode_id": "ep-trace",
        "seq": seq,
        "ts": 100.0,
        "actor_uid": "agent-main",
        "actor_role": "untrusted",
        "kind": "action",
        "payload": {"args": {}},
        "turn": 0,
        "tool": "read_file",
    }
    return Event(**{**base, **fields})  # type: ignore[arg-type]


def test_sealed_events_are_tagged_with_the_bound_turn_or_world() -> None:
    trace = AgentTrace()
    trace.on_sealed_append(_event(0, actor_uid="agent-main", kind="pr"))
    with trace.turn("serving-agent", 0):
        trace.on_sealed_append(_event(1))
        trace.on_sealed_append(_event(2, kind="inference_call"))
    trace.on_sealed_append(_event(3, kind="tick"))
    lanes = trace.finish(last_sealed_seq=3).sealed_lane
    worker = TurnRef("serving-agent", 0)
    assert dict(lanes) == {0: None, 1: worker, 2: worker, 3: None}


def test_mirror_events_are_tagged_with_the_bound_turn_or_world() -> None:
    trace = AgentTrace()
    with trace.turn("agent-main", 1):
        trace.on_mirror_append(_event(0, kind="inference_call"))
    trace.on_mirror_append(_event(1, kind="inference_call"))
    finished = trace.finish(last_sealed_seq=-1)
    assert dict(finished.mirror_lane) == {0: TurnRef("agent-main", 1), 1: None}
    assert dict(finished.mirror_to_sealed) == {}


def test_a_mirror_event_identical_to_the_last_sealed_event_is_its_twin() -> None:
    trace = AgentTrace()
    trace.on_sealed_append(_event(40, tool="write_file"))
    trace.on_mirror_append(_event(9, tool="write_file"))
    assert dict(trace.finish(last_sealed_seq=40).mirror_to_sealed) == {9: 40}


def test_an_edge_prompt_copy_after_a_sealed_inference_record_has_no_twin() -> None:
    trace = AgentTrace()
    trace.on_sealed_append(_event(40, kind="inference_call", payload={"prompt_fp": "a", "in_mirror": True}))
    trace.on_mirror_append(_event(9, kind="inference_call", payload={"prompt_fp": "a"}))
    assert dict(trace.finish(last_sealed_seq=40).mirror_to_sealed) == {}


def test_real_recorder_dual_writes_pair_up_through_the_log_subscribers(tmp_path: Path) -> None:
    trace = AgentTrace()
    sealed = AppendOnlyLog(tmp_path / "sealed.jsonl", "ep-trace", on_append=trace.on_sealed_append)
    mirror = AppendOnlyLog(tmp_path / "mirror.jsonl", "ep-trace", on_append=trace.on_mirror_append)
    recorder = Recorder("ep-trace", sealed, mirror, clock=lambda: 1.0)
    recorder.sealed(actor_uid="agent-main", actor_role="orchestrator", kind="spawn", payload={})
    with trace.turn("agent-main", 0):
        recorder.dual(actor_uid="agent-main", actor_role="orchestrator", kind="action", payload={"a": 1})
        recorder.dual(actor_uid="agent-main", actor_role="orchestrator", kind="action", payload={"a": 2})
    finished = trace.finish(last_sealed_seq=sealed.last_seq)
    main = TurnRef("agent-main", 0)
    assert dict(finished.sealed_lane) == {0: None, 1: main, 2: main}
    assert dict(finished.mirror_lane) == {0: main, 1: main}
    assert dict(finished.mirror_to_sealed) == {0: 1, 1: 2}


def test_a_sealed_event_is_the_twin_of_at_most_one_mirror_event() -> None:
    trace = AgentTrace()
    trace.on_sealed_append(_event(40, kind="message"))
    trace.on_mirror_append(_event(9, kind="message"))
    trace.on_mirror_append(_event(10, kind="message"))
    assert dict(trace.finish(last_sealed_seq=40).mirror_to_sealed) == {9: 40}


def test_a_clock_failure_on_turn_exit_still_unbinds_the_turn() -> None:
    readings: list[float | None] = [0.0, None, 2.0, 3.0]

    def failing_once() -> float:
        reading = readings.pop(0)
        if reading is None:
            raise OSError("clock unavailable")
        return reading

    trace = AgentTrace(wall_clock=failing_once)
    with pytest.raises(OSError), trace.turn("agent-main", 0):
        pass
    with trace.turn("agent-main", 1):
        pass
    assert trace.finish(last_sealed_seq=-1).turns == (TurnRecord(TurnRef("agent-main", 1), 2.0, 3.0),)
