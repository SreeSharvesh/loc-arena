"""Where each event and each monitor verdict of a run belongs: its lane, its round, the step that holds it.

A turn's events are its agent's; everything outside a turn is the ``World`` lane's. Monitors read the
mirror log, so a verdict names a mirror seq, while the Inspect log numbers events by the sealed log. A log
that records each event's ``mirror_seq`` is paired through it; an older one is paired by content, each
mirror event with its sealed twin (equal in every field but ``seq`` and ``fp``, sealed first).
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from inspect_ai.event import Event as InspectEvent
from inspect_ai.event import InfoEvent, ModelEvent, SpanBeginEvent, ToolEvent
from inspect_ai.log import EvalSample
from pydantic import JsonValue, TypeAdapter

from loc_arena.explorer.page import VerdictView
from loc_arena.explorer.scores import RunScores
from loc_arena.logging_.transcript_lanes import BEFORE_FIRST_ROUND, WORLD
from loc_arena.monitors.registry import MonitorVerdict

# A logged event is read as plain JSON: a bundle written by newer code may carry event kinds this code lacks.
LOGGED_EVENT: Final = TypeAdapter(dict[str, JsonValue])


@dataclass(frozen=True)
class VerdictPlacement:
    """Where the verdicts landed.

    `placed` holds each verdict on the sealed event it judged, keyed by that sealed seq; `unplaced` holds
    those whose target is no shown event.
    """

    placed: Mapping[int, tuple[VerdictView, ...]]
    unplaced: tuple[VerdictView, ...]


@dataclass(frozen=True)
class PlacedEvent:
    """An event of the log, and the lane and round it happened in."""

    lane: str
    round: int
    event: InspectEvent


@dataclass(frozen=True)
class EventSeqs:
    """An event's seq in the sealed log and in the mirror log, where the exporter recorded them."""

    sealed: int | None
    mirror: int | None


def place_events(sample: EvalSample) -> list[PlacedEvent]:
    """Every event with its lane and round: a turn's events are its agent's, the rest are World's.

    A World event takes the round of the latest turn begun before it, or ``BEFORE_FIRST_ROUND``.
    """
    turns = {
        event.id: (event.name.removeprefix("turn "), event.parent_id)
        for event in sample.events
        if isinstance(event, SpanBeginEvent) and event.type == "turn"
    }
    agents = {
        event.id: event.name
        for event in sample.events
        if isinstance(event, SpanBeginEvent) and event.type == "agent"
    }
    placed: list[PlacedEvent] = []
    latest_round = BEFORE_FIRST_ROUND
    for event in sample.events:
        if isinstance(event, SpanBeginEvent) and event.id in turns:
            latest_round = int(turns[event.id][0])
            continue
        if not isinstance(event, ModelEvent | ToolEvent | InfoEvent):
            continue
        turn = turns.get(event.span_id or "")
        if turn is None:
            placed.append(PlacedEvent(WORLD, latest_round, event))
        else:
            latest_round = int(turn[0])
            placed.append(PlacedEvent(agents.get(turn[1] or "", WORLD), latest_round, event))
    return placed


def order_lanes(configured: Sequence[str], present: set[str]) -> tuple[str, ...]:
    """The configured agents in their order, then any other agent, then World: only lanes with steps."""
    lanes = [*configured, *sorted(present - {WORLD} - set(configured)), WORLD]
    return tuple(lane for lane in dict.fromkeys(lanes) if lane in present)


def read_event_seqs(event: InspectEvent) -> EventSeqs:
    """An event's sealed and mirror seq, as the exporter recorded them."""
    recorded: Mapping[str, object]
    if isinstance(event, ToolEvent):
        recorded = event.metadata or {}
    elif isinstance(event, InfoEvent) and isinstance(event.data, dict):
        recorded = event.data
    else:
        return EventSeqs(sealed=None, mirror=None)
    sealed, mirror = recorded.get("seq"), recorded.get("mirror_seq")
    return EventSeqs(
        sealed=sealed if isinstance(sealed, int) else None,
        mirror=mirror if isinstance(mirror, int) else None,
    )


def list_verdict_targets(event: InspectEvent) -> list[tuple[int, str]]:
    """The sealed seq a verdict could name on this event, and what to call the thing it judged."""
    sealed = read_event_seqs(event).sealed
    if sealed is None:
        return []
    if isinstance(event, ToolEvent):
        return [(sealed, event.function)]
    return [(sealed, str(event.source or "event"))] if isinstance(event, InfoEvent) else []


def pair_mirror_to_sealed(sample: EvalSample, bundle: Path) -> Mapping[int, int]:
    """Each mirror seq's sealed twin: from the log's ``mirror_seq`` when it records one, else by content."""
    recorded = [read_event_seqs(event) for event in sample.events]
    from_log = {
        seqs.mirror: seqs.sealed for seqs in recorded if seqs.mirror is not None and seqs.sealed is not None
    }
    if from_log:
        return from_log
    return pair_by_content(
        read_logged_events(bundle / "events.sealed.jsonl"),
        read_logged_events(bundle / "events.mirror.jsonl"),
    )


def read_logged_events(path: Path) -> list[tuple[int, str]]:
    """Each event of a JSONL log as its seq, and a key over every other field but its fingerprint."""
    events: list[tuple[int, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = LOGGED_EVENT.validate_json(line)
        seq = record.get("seq")
        if not isinstance(seq, int):
            raise ValueError(f"{path}: an event without an integer seq: {line[:120]}")
        events.append(
            (seq, json.dumps({k: v for k, v in record.items() if k not in ("seq", "fp")}, sort_keys=True)),
        )
    return events


def pair_by_content(
    sealed_events: Iterable[tuple[int, str]],
    mirror_events: Iterable[tuple[int, str]],
) -> Mapping[int, int]:
    """Pair each mirror event with the first unpaired sealed event of the same content key."""
    waiting: dict[str, deque[int]] = defaultdict(deque)
    for seq, key in sealed_events:
        waiting[key].append(seq)
    pairs: dict[int, int] = {}
    for seq, key in mirror_events:
        twins = waiting.get(key)
        if twins:
            pairs[seq] = twins.popleft()
    return pairs


def place_verdicts(
    scores: RunScores,
    mirror_to_sealed: Mapping[int, int],
    targets: Mapping[int, tuple[str, str]],
) -> VerdictPlacement:
    """Each verdict on the sealed twin of the mirror event it names; a verdict with no shown twin is unplaced.

    `targets` maps a sealed seq to the step that holds it and what to call the thing judged there.
    """
    placed: dict[int, list[VerdictView]] = defaultdict(list)
    unplaced: list[VerdictView] = []
    for verdict in scores.monitor_verdicts:
        sealed = mirror_to_sealed.get(verdict.target_seq) if verdict.target_seq is not None else None
        if sealed is None or sealed not in targets:
            unplaced.append(_describe_verdict(verdict, None, None))
            continue
        step, target = targets[sealed]
        placed[sealed].append(_describe_verdict(verdict, step, target))
    return VerdictPlacement({seq: tuple(views) for seq, views in placed.items()}, tuple(unplaced))


def _describe_verdict(verdict: MonitorVerdict, step: str | None, target: str | None) -> VerdictView:
    return VerdictView(
        monitor=verdict.monitor,
        tap=verdict.tap,
        score=verdict.score,
        reason=verdict.reason,
        actor=verdict.actor_uid,
        target_mirror_seq=verdict.target_seq,
        step=step,
        target=target,
    )
