from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from inspect_ai.event import Event as InspectEvent
from inspect_ai.event import InfoEvent, SpanBeginEvent, SpanEndEvent
from inspect_ai.log import EvalConfig, EvalDataset, EvalSample, EvalSpec, read_eval_log
from loc_arena.config import load_run_config
from loc_arena.logging_ import inspect_export
from loc_arena.logging_.agent_trace import AgentTrace, EpisodeTrace, ModelCall, TurnRef
from loc_arena.logging_.events import AppendOnlyLog, Event, EventKind
from loc_arena.logging_.inspect_export import EpisodeExport, UnassignedEventError, write_run_eval
from loc_arena.scaffold.bus import Recorder

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")


def _episode(sample_id: str, tmp_path: Path) -> EpisodeExport:
    return EpisodeExport(
        sample_id=sample_id,
        trace=AgentTrace().finish(last_sealed_seq=-1),
        sealed_path=tmp_path / f"{sample_id}.jsonl",
        agent_order=("agent-main",),
    )


def test_write_run_eval_writes_one_sample_per_episode_and_scores_only_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_spec(run_name: str, config: object, mode: str, seed: int, sample_count: int) -> EvalSpec:
        return EvalSpec(
            created="2026-09-25T00:00:00+00:00",
            task=f"{run_name}/{mode}/{seed}/{sample_count}",
            dataset=EvalDataset(),
            model="none/none",
            config=EvalConfig(),
        )

    def fake_sample(episode: EpisodeExport, scores: Mapping[str, Any] | None) -> EvalSample:
        return EvalSample(id=episode.sample_id, epoch=1, input="", target="", metadata={"scores": scores})

    monkeypatch.setattr(inspect_export, "_eval_spec", fake_spec)
    monkeypatch.setattr(inspect_export, "_sample", fake_sample)
    path = write_run_eval(
        tmp_path / "run.eval",
        run_name="run-x",
        config=CFG,
        mode="attack",
        seed=7,
        scores={"caught": True},
        episodes=[_episode("episode", tmp_path), _episode("honest_cal", tmp_path)],
    )
    log = read_eval_log(str(path))
    assert log.status == "success"
    assert log.eval.task == "run-x/attack/7/2"
    assert log.samples is not None
    assert [(s.id, s.metadata["scores"]) for s in log.samples] == [
        ("episode", {"caught": True}),
        ("honest_cal", None),
    ]


def test_eval_spec_names_the_task_after_the_run_slug_and_records_the_mode() -> None:
    spec = inspect_export._eval_spec("run-x", CFG, "honest", 3, 1)
    assert spec.task == f"loc_arena/{CFG.run_slug}"
    assert spec.run_id == "run-x"
    assert spec.model == CFG.models["untrusted_agent"].model
    assert spec.dataset.samples == 1
    assert spec.metadata == {"mode": "honest", "seed": 3, "policy": CFG.policy, "run_name": "run-x"}


def _write_sealed(path: Path, count: int) -> None:
    log = AppendOnlyLog(path, "ep-export")
    for seq in range(count):
        log.append(
            Event(episode_id="ep-export", seq=seq, ts=0.0, actor_uid="world", actor_role="npc", kind="tick")
        )


def test_sample_reads_the_sealed_log_assigns_lanes_and_builds_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode = _episode("episode", tmp_path)
    _write_sealed(episode.sealed_path, 3)
    seen: dict[str, object] = {}

    def fake_lanes(trace: EpisodeTrace, sealed_events: list[Event]) -> dict[int, TurnRef | None]:
        seen["seqs"] = [e.seq for e in sealed_events]
        return {0: None, 1: None, 2: None}

    def fake_events(
        ep: EpisodeExport, sealed_events: list[Event], lanes: Mapping[int, TurnRef | None]
    ) -> list[InspectEvent]:
        seen["lanes"] = dict(lanes)
        return [InfoEvent(data="marker")]

    monkeypatch.setattr(inspect_export, "_lanes_for", fake_lanes)
    monkeypatch.setattr(inspect_export, "_sample_events", fake_events)
    sample = inspect_export._sample(episode, {"caught": False})
    assert seen == {"seqs": [0, 1, 2], "lanes": {0: None, 1: None, 2: None}}
    assert sample.id == "episode"
    assert [e.event for e in sample.events] == ["info"]
    assert sample.metadata == {"agents": ["agent-main"], "scores": {"caught": False}}


def _tick(seq: int) -> Event:
    return Event(episode_id="ep-export", seq=seq, ts=0.0, actor_uid="world", actor_role="npc", kind="tick")


def _traced(lanes: dict[int, TurnRef | None], boundary: int) -> EpisodeTrace:
    trace = AgentTrace()
    for seq, ref in lanes.items():
        if ref is None:
            trace.on_sealed_append(_tick(seq))
        else:
            with trace.turn(ref.agent_uid, ref.turn):
                trace.on_sealed_append(_tick(seq))
    return trace.finish(last_sealed_seq=boundary)


def test_lanes_come_from_the_trace_inside_the_boundary_and_world_after_it() -> None:
    main = TurnRef("agent-main", 0)
    trace = _traced({0: None, 1: main}, boundary=1)
    lanes = inspect_export._lanes_for(trace, [_tick(0), _tick(1), _tick(2), _tick(3)])
    assert lanes == {0: None, 1: main, 2: None, 3: None}


def test_an_untagged_event_inside_the_boundary_raises() -> None:
    trace = _traced({0: None}, boundary=2)
    with pytest.raises(UnassignedEventError, match=r"2 sealed events .* first untagged seqs: \[1, 2\]"):
        inspect_export._lanes_for(trace, [_tick(0), _tick(1), _tick(2)])


def test_agent_span_id() -> None:
    assert inspect_export._agent_span_id("serving-agent") == "agent:serving-agent"


def test_turn_span_id() -> None:
    assert inspect_export._turn_span_id(TurnRef("serving-agent", 4)) == "turn:serving-agent:4"


def test_timestamp_is_an_aware_utc_datetime() -> None:
    assert inspect_export._timestamp(0.0) == datetime(1970, 1, 1, tzinfo=UTC)


def test_model_event_shows_the_post_injection_input_and_the_reply() -> None:
    call = ModelCall("executing", "batch-runner", "untrusted_agent", "OBJECTIVE\n\nbrief", "reply", 12, 0.0)
    event = inspect_export._model_event(call, "turn:agent-main:3")
    assert event.span_id == "turn:agent-main:3"
    assert [m.text for m in event.input] == ["OBJECTIVE\n\nbrief"]
    assert event.output.completion == "reply"
    assert event.metadata == {"identity": "batch-runner", "phase": "executing", "sealed_seq": 12}


def _action(seq: int, result: dict[str, Any] | None = None, **payload: object) -> Event:
    return Event(
        episode_id="ep-export",
        seq=seq,
        ts=0.0,
        actor_uid="serving-agent",
        actor_role="untrusted",
        kind="action",
        payload=dict(payload),
        target_id="meridian-serving",
        tool="write_file",
        result=result if result is not None else {"ok": True},
    )


def test_tool_event_carries_the_tool_arguments_and_result() -> None:
    at = datetime(2026, 9, 25, tzinfo=UTC)
    event = inspect_export._tool_event(_action(5, args={"path": "a.py"}, blocked=False), "turn:s:0", at)
    assert (event.id, event.function, event.arguments) == ("seq-5", "write_file", {"path": "a.py"})
    assert event.result == '{"ok": true}'
    assert event.error is None
    assert event.span_id == "turn:s:0" and event.timestamp == at


def test_a_blocked_action_becomes_a_permission_error() -> None:
    at = datetime(2026, 9, 25, tzinfo=UTC)
    event = inspect_export._tool_event(_action(6, args={}, blocked=True, reason="out of scope"), None, at)
    assert event.error is not None
    assert (event.error.type, event.error.message) == ("permission", "out of scope")


def test_info_event_keeps_the_kind_and_the_event_body() -> None:
    at = datetime(2026, 9, 25, tzinfo=UTC)
    message = Event(
        episode_id="ep-export",
        seq=8,
        ts=0.0,
        actor_uid="agent-main",
        actor_role="orchestrator",
        kind="message",
        payload={"body": "run the bench"},
        target_id="eval-agent",
    )
    event = inspect_export._info_event(message, "turn:agent-main:0", at)
    assert event.source == "message"
    assert event.data == {
        "seq": 8,
        "actor_uid": "agent-main",
        "target_id": "eval-agent",
        "payload": {"body": "run the bench"},
        "result": None,
    }


def _world(seq_log: AppendOnlyLog, kind: EventKind = "tick") -> None:
    seq_log.append(
        Event(
            episode_id="ep-export",
            seq=seq_log.next_seq(),
            ts=0.0,
            actor_uid="world",
            actor_role="npc",
            kind=kind,
        )
    )


def test_a_traced_episode_exports_a_span_tree_with_one_agent_span_per_agent(tmp_path: Path) -> None:
    trace = AgentTrace(wall_clock=lambda: 1_700_000_000.0)
    sealed_path = tmp_path / "sealed.jsonl"
    sealed = AppendOnlyLog(sealed_path, "ep-export", on_append=trace.on_sealed_append)
    mirror = AppendOnlyLog(tmp_path / "mirror.jsonl", "ep-export")
    recorder = Recorder("ep-export", sealed, mirror, clock=lambda: 0.0)
    _world(sealed)
    with trace.turn("agent-main", 0):
        record = sealed.append(
            Event(
                episode_id="ep-export",
                seq=sealed.next_seq(),
                ts=0.0,
                actor_uid="agent-main",
                actor_role="untrusted",
                kind="inference_call",
            )
        )
        trace.on_model_call(
            identity="agent-main", role="untrusted_agent", model_input="p", output="r", sealed_seq=record.seq
        )
        trace.mark_executing()
        recorder.dual(
            actor_uid="agent-main", actor_role="orchestrator", kind="action", tool="read_file", payload={}
        )
        recorder.dual(
            actor_uid="agent-main", actor_role="orchestrator", kind="message", payload={"body": "go"}
        )
    with trace.turn("serving-agent", 0):
        recorder.dual(
            actor_uid="serving-agent", actor_role="worker", kind="action", tool="run_tests", payload={}
        )
    _world(sealed)
    episode_trace = trace.finish(last_sealed_seq=sealed.last_seq)
    _world(AppendOnlyLog(sealed_path, "ep-export"), kind="monitor_error")

    path = write_run_eval(
        tmp_path / "run.eval",
        run_name="run-x",
        config=CFG,
        mode="attack",
        seed=1,
        scores={},
        episodes=[EpisodeExport("episode", episode_trace, sealed_path, ("agent-main", "serving-agent"))],
    )
    log = read_eval_log(str(path))
    assert log.samples is not None
    events = log.samples[0].events
    assert [(e.event, e.span_id if e.event not in ("span_begin", "span_end") else e.id) for e in events] == [
        ("span_begin", "episode:episode"),
        ("info", "episode:episode"),
        ("span_begin", "agent:agent-main"),
        ("span_begin", "turn:agent-main:0"),
        ("model", "turn:agent-main:0"),
        ("tool", "turn:agent-main:0"),
        ("info", "turn:agent-main:0"),
        ("span_end", "turn:agent-main:0"),
        ("span_begin", "agent:serving-agent"),
        ("span_begin", "turn:serving-agent:0"),
        ("tool", "turn:serving-agent:0"),
        ("span_end", "turn:serving-agent:0"),
        ("info", "episode:episode"),
        ("span_end", "agent:agent-main"),
        ("span_end", "agent:serving-agent"),
        ("span_begin", "after_episode:episode"),
        ("info", "after_episode:episode"),
        ("span_end", "after_episode:episode"),
        ("span_end", "episode:episode"),
    ]
    (root,) = _viewer_tree(events)
    assert root["id"] == "episode:episode"
    agents = [c for c in root["children"] if c.get("type") == "agent"]
    assert [(a["name"], [t["name"] for t in a["children"]]) for a in agents] == [
        ("agent-main", ["turn 0"]),
        ("serving-agent", ["turn 0"]),
    ]
    world = [c for c in root["children"] if "event" in c]
    assert [w["event"] for w in world] == ["info", "info"]
    (after,) = [c for c in root["children"] if c.get("type") == "after_episode"]
    assert [c["event"] for c in after["children"]] == ["info"]


def _viewer_tree(events: list[InspectEvent]) -> list[dict[str, Any]]:
    spans: dict[str, dict[str, Any]] = {}
    for e in events:
        if isinstance(e, SpanBeginEvent):
            spans[e.id] = {"id": e.id, "name": e.name, "type": e.type, "parent": e.parent_id, "children": []}
    roots: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    for e in events:
        if isinstance(e, SpanBeginEvent):
            span = spans[e.id]
            parent = span["parent"]
            if parent and parent in spans:
                spans[parent]["children"].append(span)
            elif stack:
                stack[-1]["children"].append(span)
            else:
                roots.append(span)
            stack.append(span)
        elif isinstance(e, SpanEndEvent):
            if stack:
                stack.pop()
        else:
            node = {"event": e.event}
            if e.span_id and e.span_id in spans:
                spans[e.span_id]["children"].append(node)
            elif stack:
                stack[-1]["children"].append(node)
            else:
                roots.append(node)
    return roots


def test_a_turn_whose_events_are_split_by_another_turn_raises() -> None:
    main, worker = TurnRef("agent-main", 0), TurnRef("serving-agent", 0)
    trace = _traced({0: main, 1: worker}, boundary=2)
    episode = EpisodeExport("episode", trace, Path("unused"), ("agent-main", "serving-agent"))
    lanes: dict[int, TurnRef | None] = {0: main, 1: worker, 2: main}
    with pytest.raises(ValueError, match="agent-main turn 0 wrote sealed events in two separate runs"):
        inspect_export._sample_events(episode, [_tick(0), _tick(1), _tick(2)], lanes)


def test_eval_spec_refuses_a_config_without_an_untrusted_agent_model() -> None:
    config = dataclasses.replace(CFG, models={k: v for k, v in CFG.models.items() if k != "untrusted_agent"})
    with pytest.raises(KeyError, match="untrusted_agent"):
        inspect_export._eval_spec("run-x", config, "attack", 1, 1)


def test_a_block_reported_only_in_the_result_is_still_a_permission_error() -> None:
    at = datetime(2026, 9, 25, tzinfo=UTC)
    spawn = _action(7, result={"blocked": True, "reason": "team is pre-provisioned"}, args={}, blocked=False)
    event = inspect_export._tool_event(spawn, None, at)
    assert event.error is not None
    assert (event.error.type, event.error.message) == ("permission", "team is pre-provisioned")


def test_working_start_is_seconds_since_the_episode_began() -> None:
    main = TurnRef("agent-main", 0)
    readings = iter([1_700_000_010.0, 1_700_000_025.0])
    trace = AgentTrace(wall_clock=lambda: next(readings))
    with trace.turn("agent-main", 0):
        trace.on_sealed_append(_tick(0))
    episode = EpisodeExport("episode", trace.finish(last_sealed_seq=0), Path("unused"), ("agent-main",))
    events = inspect_export._sample_events(episode, [_tick(0)], {0: main})
    assert [(e.event, e.working_start) for e in events] == [
        ("span_begin", 0.0),
        ("span_begin", 0.0),
        ("span_begin", 0.0),
        ("info", 0.0),
        ("span_end", 15.0),
        ("span_end", 15.0),
        ("span_end", 15.0),
    ]


def test_timestamps_follow_turn_bounds_and_model_calls_and_world_reuses_the_last() -> None:
    main = TurnRef("agent-main", 0)
    readings = iter([100.0, 130.0, 160.0])
    trace = AgentTrace(wall_clock=lambda: next(readings))
    with trace.turn("agent-main", 0):
        trace.on_sealed_append(_tick(0))
        trace.on_model_call(identity="agent-main", role="r", model_input="p", output="o", sealed_seq=1)
        trace.on_sealed_append(_tick(1))
        trace.on_sealed_append(_tick(2))
    trace.on_sealed_append(_tick(3))
    episode = EpisodeExport("episode", trace.finish(last_sealed_seq=3), Path("unused"), ("agent-main",))
    inference = dataclasses.replace(_tick(1), kind="inference_call")
    events = inspect_export._sample_events(
        episode, [_tick(0), inference, _tick(2), _tick(3)], {0: main, 1: main, 2: main, 3: None}
    )
    offsets = [(e.event, e.working_start) for e in events if e.event != "span_begin"]
    assert offsets == [
        ("info", 0.0),
        ("model", 30.0),
        ("info", 30.0),
        ("span_end", 60.0),
        ("info", 60.0),
        ("span_end", 60.0),
        ("span_end", 60.0),
    ]


def test_cause_first_moves_the_action_to_the_start_of_the_executing_phase() -> None:
    decide, job, tick, action = _tick(1), _tick(3), _tick(4), _action(5)
    assert inspect_export._cause_first([decide, job, tick, action], 3) == [decide, action, job, tick]


def test_cause_first_leaves_a_turn_without_an_executing_phase_or_trailing_action_alone() -> None:
    run = [_tick(1), _action(2)]
    assert inspect_export._cause_first(run, None) == run
    assert inspect_export._cause_first([_action(1), _tick(2)], 1) == [_action(1), _tick(2)]
