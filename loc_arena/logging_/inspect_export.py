"""Write one run as a real Inspect ``.eval`` log, a span per agent; contract in spec.md beside this module."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import groupby
from pathlib import Path
from typing import Any

from inspect_ai.event import Event as InspectEvent
from inspect_ai.event import InfoEvent, ModelEvent, SpanBeginEvent, SpanEndEvent, ToolEvent
from inspect_ai.log import EvalConfig, EvalDataset, EvalLog, EvalSample, EvalSpec, write_eval_log
from inspect_ai.model import ChatMessageUser, GenerateConfig, ModelOutput
from inspect_ai.tool import ToolCallError

from loc_arena.config import RunConfig
from loc_arena.logging_.agent_trace import EpisodeTrace, ModelCall, TurnRef
from loc_arena.logging_.events import Event, read_events


class UnassignedEventError(RuntimeError):
    """A sealed event inside the episode boundary has no lane."""


@dataclass(frozen=True)
class EpisodeExport:
    """One episode to export as one Inspect sample."""

    sample_id: str
    trace: EpisodeTrace
    sealed_path: Path
    agent_order: tuple[str, ...]


def write_run_eval(
    path: Path,
    *,
    run_name: str,
    config: RunConfig,
    mode: str,
    seed: int,
    scores: Mapping[str, Any],
    episodes: Sequence[EpisodeExport],
) -> Path:
    log = EvalLog(
        status="success",
        eval=_eval_spec(run_name, config, mode, seed, len(episodes)),
        samples=[_sample(episode, scores if index == 0 else None) for index, episode in enumerate(episodes)],
    )
    write_eval_log(log, str(path))
    return path


def _eval_spec(run_name: str, config: RunConfig, mode: str, seed: int, sample_count: int) -> EvalSpec:
    return EvalSpec(
        created=datetime.now(UTC).isoformat(),
        task=f"loc_arena/{config.run_slug}",
        task_id=config.run_slug,
        run_id=run_name,
        dataset=EvalDataset(name=config.run_slug, samples=sample_count),
        model=config.models["untrusted_agent"].model,
        config=EvalConfig(),
        metadata={"mode": mode, "seed": seed, "policy": config.policy, "run_name": run_name},
    )


def _sample(episode: EpisodeExport, scores: Mapping[str, Any] | None) -> EvalSample:
    sealed_events = list(read_events(episode.sealed_path))
    lanes = _lanes_for(episode.trace, sealed_events)
    return EvalSample(
        id=episode.sample_id,
        epoch=1,
        input=f"LOC-Arena {episode.sample_id}",
        target="",
        events=_sample_events(episode, sealed_events, lanes),
        metadata={
            "agents": list(episode.agent_order),
            "scores": dict(scores) if scores is not None else None,
        },
    )


def _lanes_for(trace: EpisodeTrace, sealed_events: Sequence[Event]) -> dict[int, TurnRef | None]:
    lanes: dict[int, TurnRef | None] = {}
    untagged: list[int] = []
    for event in sealed_events:
        if event.seq > trace.last_sealed_seq:
            lanes[event.seq] = None
        elif event.seq in trace.sealed_lane:
            lanes[event.seq] = trace.sealed_lane[event.seq]
        else:
            untagged.append(event.seq)
    if untagged:
        raise UnassignedEventError(
            f"{len(untagged)} sealed events at or before the episode boundary "
            f"(seq {trace.last_sealed_seq}) have no lane; first untagged seqs: {untagged[:5]}"
        )
    return lanes


def _sample_events(
    episode: EpisodeExport, sealed_events: Sequence[Event], lanes: Mapping[int, TurnRef | None]
) -> list[InspectEvent]:
    turn_records = {record.ref: record for record in episode.trace.turns}
    calls = {call.sealed_seq: call for call in episode.trace.model_calls}
    wall_readings = [r.wall_start for r in episode.trace.turns] + [
        c.wall_ts for c in episode.trace.model_calls
    ]
    now = _timestamp(min(wall_readings)) if wall_readings else datetime.now(UTC)
    root_id = _episode_span_id(episode.sample_id)
    events: list[InspectEvent] = [
        SpanBeginEvent(id=root_id, name=episode.sample_id, type="episode", timestamp=now)
    ]
    opened_agents: list[str] = []
    finished_turns: set[TurnRef] = set()
    boundary = episode.trace.last_sealed_seq
    in_episode = [sealed for sealed in sealed_events if sealed.seq <= boundary]
    after_episode = [sealed for sealed in sealed_events if sealed.seq > boundary]
    for lane, run in groupby(in_episode, key=lambda sealed: lanes[sealed.seq]):
        if lane is not None:
            if lane in finished_turns:
                raise ValueError(
                    f"{lane.agent_uid} turn {lane.turn} wrote sealed events in two separate runs; "
                    "turns must not interleave"
                )
            now = _timestamp(turn_records[lane].wall_start)
            if lane.agent_uid not in opened_agents:
                opened_agents.append(lane.agent_uid)
                events.append(
                    SpanBeginEvent(
                        id=_agent_span_id(lane.agent_uid),
                        parent_id=root_id,
                        name=lane.agent_uid,
                        type="agent",
                        timestamp=now,
                    )
                )
            events.append(
                SpanBeginEvent(
                    id=_turn_span_id(lane),
                    parent_id=_agent_span_id(lane.agent_uid),
                    name=f"turn {lane.turn}",
                    type="turn",
                    timestamp=now,
                )
            )
        span_id = _turn_span_id(lane) if lane is not None else root_id
        events_in_run = list(run)
        ordered = (
            _cause_first(events_in_run, turn_records[lane].executing_from_seq)
            if lane is not None
            else events_in_run
        )
        for event in ordered:
            call = calls.get(event.seq)
            if event.kind == "inference_call" and call is not None:
                now = _timestamp(call.wall_ts)
                events.append(_model_event(call, span_id))
            elif event.kind == "action":
                events.append(_tool_event(event, span_id, now))
            else:
                events.append(_info_event(event, span_id, now))
        if lane is not None:
            now = _timestamp(turn_records[lane].wall_end)
            events.append(SpanEndEvent(id=_turn_span_id(lane), timestamp=now))
            finished_turns.add(lane)
    events.extend(SpanEndEvent(id=_agent_span_id(uid), timestamp=now) for uid in opened_agents)
    if after_episode:
        after_id = _after_episode_span_id(episode.sample_id)
        events.append(
            SpanBeginEvent(
                id=after_id, parent_id=root_id, name="after episode", type="after_episode", timestamp=now
            )
        )
        for event in after_episode:
            now = max(now, _timestamp(event.ts))
            events.append(_info_event(event, after_id, now))
        events.append(SpanEndEvent(id=after_id, timestamp=now))
    events.append(SpanEndEvent(id=root_id, timestamp=now))
    started = events[0].timestamp
    for inspect_event in events:
        inspect_event.working_start = (inspect_event.timestamp - started).total_seconds()
    return events


def _cause_first(run: list[Event], executing_from_seq: int | None) -> list[Event]:
    if executing_from_seq is None or not run or run[-1].kind != "action":
        return run
    split = next((index for index, event in enumerate(run) if event.seq >= executing_from_seq), len(run) - 1)
    return [*run[:split], run[-1], *run[split:-1]]


def _after_episode_span_id(sample_id: str) -> str:
    return f"after_episode:{sample_id}"


def _episode_span_id(sample_id: str) -> str:
    return f"episode:{sample_id}"


def _agent_span_id(agent_uid: str) -> str:
    return f"agent:{agent_uid}"


def _turn_span_id(ref: TurnRef) -> str:
    return f"turn:{ref.agent_uid}:{ref.turn}"


def _model_event(call: ModelCall, span_id: str | None) -> ModelEvent:
    at = _timestamp(call.wall_ts)
    return ModelEvent(
        model=call.role,
        role=call.role,
        input=[ChatMessageUser(content=call.model_input)],
        tools=[],
        tool_choice="none",
        config=GenerateConfig(),
        output=ModelOutput.from_content(model=call.role, content=call.output),
        span_id=span_id,
        timestamp=at,
        completed=at,
        metadata={"identity": call.identity, "phase": call.phase, "sealed_seq": call.sealed_seq},
    )


def _tool_event(event: Event, span_id: str | None, at: datetime) -> ToolEvent:
    args = event.payload.get("args")
    result = event.result or {}
    blocked = bool(event.payload.get("blocked") or result.get("blocked"))
    reason = event.payload.get("reason") or result.get("reason") or "blocked"
    return ToolEvent(
        id=f"seq-{event.seq}",
        function=event.tool or "action",
        arguments=dict(args) if isinstance(args, dict) else {},
        result=json.dumps(event.result, sort_keys=True) if event.result is not None else "",
        error=ToolCallError("permission", str(reason)) if blocked else None,
        span_id=span_id,
        timestamp=at,
        metadata={"seq": event.seq, "actor_uid": event.actor_uid, "target": event.target_id},
    )


def _info_event(event: Event, span_id: str | None, at: datetime) -> InfoEvent:
    return InfoEvent(
        source=event.kind,
        data={
            "seq": event.seq,
            "actor_uid": event.actor_uid,
            "target_id": event.target_id,
            "payload": event.payload,
            "result": event.result,
        },
        span_id=span_id,
        timestamp=at,
    )


def _timestamp(wall: float) -> datetime:
    return datetime.fromtimestamp(wall, tz=UTC)
