"""Write one run as a real Inspect ``.eval`` log with a span per agent; contract in docs/agent-log/spec.md."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from inspect_ai.event import Event as InspectEvent
from inspect_ai.event import InfoEvent, ModelEvent, SpanBeginEvent, SpanEndEvent, ToolEvent
from inspect_ai.log import EvalConfig, EvalDataset, EvalLog, EvalSample, EvalSpec, write_eval_log

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
    agent_model = config.models.get("untrusted_agent")
    return EvalSpec(
        created=datetime.now(UTC).isoformat(),
        task=f"loc_arena/{config.run_slug}",
        task_id=config.run_slug,
        run_id=run_name,
        dataset=EvalDataset(name=config.run_slug, samples=sample_count),
        model=agent_model.model if agent_model is not None else "none/none",
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
    events: list[InspectEvent] = []
    opened_agents: list[str] = []
    current: TurnRef | None = None
    for event in sealed_events:
        lane = lanes[event.seq]
        if lane != current:
            if current is not None:
                if current in turn_records:
                    now = _timestamp(turn_records[current].wall_end)
                events.append(SpanEndEvent(id=_turn_span_id(current), timestamp=now))
            if lane is not None:
                if lane in turn_records:
                    now = _timestamp(turn_records[lane].wall_start)
                if lane.agent_uid not in opened_agents:
                    opened_agents.append(lane.agent_uid)
                    events.append(
                        SpanBeginEvent(
                            id=_agent_span_id(lane.agent_uid),
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
            current = lane
        span_id = _turn_span_id(current) if current is not None else None
        call = calls.get(event.seq)
        if event.kind == "inference_call" and call is not None:
            now = _timestamp(call.wall_ts)
            events.append(_model_event(call, span_id))
        elif event.kind == "action":
            events.append(_tool_event(event, span_id, now))
        else:
            events.append(_info_event(event, span_id, now))
    if current is not None:
        if current in turn_records:
            now = _timestamp(turn_records[current].wall_end)
        events.append(SpanEndEvent(id=_turn_span_id(current), timestamp=now))
    events.extend(SpanEndEvent(id=_agent_span_id(uid), timestamp=now) for uid in opened_agents)
    return events


def _agent_span_id(agent_uid: str) -> str:
    raise NotImplementedError


def _turn_span_id(ref: TurnRef) -> str:
    raise NotImplementedError


def _model_event(call: ModelCall, span_id: str | None) -> ModelEvent:
    raise NotImplementedError


def _tool_event(event: Event, span_id: str | None, at: datetime) -> ToolEvent:
    raise NotImplementedError


def _info_event(event: Event, span_id: str | None, at: datetime) -> InfoEvent:
    raise NotImplementedError


def _timestamp(wall: float) -> datetime:
    raise NotImplementedError
