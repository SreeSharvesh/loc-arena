"""Inspect sample -> per-agent lanes of blocks, one row per round; contract in docs/agent-log/spec.md."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from inspect_ai.event import Event as InspectEvent
from inspect_ai.event import InfoEvent, ModelEvent, SpanBeginEvent, ToolEvent
from inspect_ai.log import EvalSample, resolve_sample_attachments
from pydantic import TypeAdapter

from loc_arena.monitors.registry import MonitorVerdict

WORLD = "World"
BEFORE_FIRST_ROUND = -1
UNPLACED = -2  # the row of verdicts whose target is no event in the transcript
MAX_SCORE_DECIMALS = 3  # the harness rounds per_monitor_max_score to this many decimals

_VERDICTS = TypeAdapter(list[MonitorVerdict])

BlockKind = Literal["prompt", "reply", "tool", "info", "verdict"]


@dataclass(frozen=True)
class Block:
    """One rendered unit inside a lane cell."""

    kind: BlockKind
    title: str
    body: str
    code: str | None = None
    blocked: bool = False


@dataclass(frozen=True)
class SampleTranscript:
    """One sample laid out as lanes (columns) by rounds (rows)."""

    sample_id: str
    lanes: tuple[str, ...]
    rows: tuple[int, ...]
    cells: Mapping[tuple[str, int], tuple[Block, ...]]


def build_transcript(sample: EvalSample) -> SampleTranscript:
    sample = resolve_sample_attachments(sample, "full")
    owners = _turn_owners(sample.events)
    cells: dict[tuple[str, int], list[Block]] = {}
    seen: list[str] = []
    mirror_rows: dict[int, int] = {}
    latest_round = BEFORE_FIRST_ROUND
    for event in sample.events:
        if isinstance(event, SpanBeginEvent) and event.type == "turn":
            latest_round = owners[event.id][1]
        blocks = _blocks(event)
        if not blocks:
            continue
        owner = owners.get(event.span_id or "")
        if owner is None:
            lane, row = WORLD, latest_round
        else:
            lane, row = owner
            latest_round = row
            if lane not in seen:
                seen.append(lane)
        cells.setdefault((lane, row), []).extend(blocks)
        mirror_seq = _mirror_seq(event)
        if mirror_seq is not None:
            mirror_rows[mirror_seq] = row
    verdict_cells = _verdict_cells(sample.metadata.get("scores"), mirror_rows)
    monitors = tuple(dict.fromkeys(monitor for monitor, _ in verdict_cells))
    all_cells = {**{key: tuple(blocks) for key, blocks in cells.items()}, **verdict_cells}
    return SampleTranscript(
        sample_id=str(sample.id),
        lanes=_lane_order(sample.metadata.get("agents", []), seen, monitors),
        rows=tuple(sorted({row for _, row in all_cells})),
        cells=MappingProxyType(all_cells),
    )


def _mirror_seq(event: InspectEvent) -> int | None:
    if isinstance(event, ToolEvent):
        seq = (event.metadata or {}).get("mirror_seq")
    elif isinstance(event, InfoEvent) and isinstance(event.data, Mapping):
        seq = event.data.get("mirror_seq")
    else:
        seq = None
    return seq if isinstance(seq, int) else None


def _verdict_cells(
    scores: object, mirror_rows: Mapping[int, int]
) -> dict[tuple[str, int], tuple[Block, ...]]:
    caught_scores = _caught_monitor_scores(scores)
    cells: dict[tuple[str, int], list[Block]] = {}
    for verdict in _monitor_verdicts(scores):
        row = UNPLACED if verdict.target_seq is None else mirror_rows.get(verdict.target_seq, UNPLACED)
        caught = _is_the_catch(verdict, caught_scores)
        cells.setdefault((verdict.monitor, row), []).append(_verdict_block(verdict, caught=caught))
    return {key: tuple(blocks) for key, blocks in cells.items()}


def _monitor_verdicts(scores: object) -> list[MonitorVerdict]:
    if not isinstance(scores, Mapping):
        return []
    return _VERDICTS.validate_python(scores.get("monitor_verdicts", []))


def _caught_monitor_scores(scores: object) -> Mapping[str, float]:
    safety = scores.get("safety") if isinstance(scores, Mapping) else None
    if not isinstance(safety, Mapping):
        return {}
    caught_by, max_scores = safety.get("caught_by"), safety.get("per_monitor_max_score")
    if not isinstance(caught_by, list) or not isinstance(max_scores, Mapping):
        return {}
    return {m: max_scores[m] for m in caught_by if m in max_scores}


def _is_the_catch(verdict: MonitorVerdict, caught_scores: Mapping[str, float]) -> bool:
    return caught_scores.get(verdict.monitor) == round(verdict.score, MAX_SCORE_DECIMALS)


def _verdict_block(verdict: MonitorVerdict, *, caught: bool) -> Block:
    body = f"score {verdict.score:.{MAX_SCORE_DECIMALS}f}  actor {verdict.actor_uid}\n{verdict.reason}"
    title = f"monitor: {verdict.monitor} ({verdict.tap})" + (" -- CAUGHT" if caught else "")
    return Block("verdict", title, body, blocked=caught)


def _turn_owners(events: Sequence[InspectEvent]) -> dict[str, tuple[str, int]]:
    spans = {event.id: event for event in events if isinstance(event, SpanBeginEvent)}
    turns: dict[str, tuple[str, int]] = {}
    for span in spans.values():
        if span.type != "turn":
            continue
        agent = spans.get(span.parent_id or "")
        round_text = span.name.removeprefix("turn ")
        if (
            agent is None
            or agent.type != "agent"
            or not span.name.startswith("turn ")
            or not round_text.isdigit()
        ):
            raise ValueError(f"turn span {span.id!r} is not a 'turn <n>' span under an agent span")
        turns[span.id] = (agent.name, int(round_text))
    owners: dict[str, tuple[str, int]] = {}
    for span_id in spans:
        ancestor: str | None = span_id
        visited: set[str] = set()
        while ancestor is not None and ancestor not in turns and ancestor not in visited:
            visited.add(ancestor)
            parent = spans.get(ancestor)
            ancestor = parent.parent_id if parent is not None else None
        if ancestor is not None and ancestor in turns:
            owners[span_id] = turns[ancestor]
    return owners


def _lane_order(configured: Sequence[str], seen: Sequence[str], monitors: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys([WORLD, *monitors, *configured, *seen]))


def _blocks(event: InspectEvent) -> tuple[Block, ...]:
    if isinstance(event, ModelEvent):
        return _model_blocks(event)
    if isinstance(event, ToolEvent):
        return (_tool_block(event),)
    if isinstance(event, InfoEvent):
        return (_info_block(event),)
    return ()


def _model_blocks(event: ModelEvent) -> tuple[Block, ...]:
    metadata = event.metadata or {}
    phase = metadata.get("phase")
    caller = f"prompt as {metadata.get('identity', event.role)}"
    prompt = "\n\n".join(message.text for message in event.input)
    return (
        Block("prompt", f"{caller} ({phase})" if phase else caller, prompt),
        Block("reply", "reply (error)", event.error)
        if event.error
        else Block("reply", "reply", event.output.completion),
    )


def _tool_block(event: ToolEvent) -> Block:
    arguments = json.dumps(event.arguments, indent=2, sort_keys=True, ensure_ascii=False)
    result = str(event.result)
    if event.error is not None:
        body = f"{event.error.message}\n{result}" if result else event.error.message
        return Block("tool", f"{event.function} (blocked)", body, code=arguments, blocked=True)
    return Block("tool", event.function, result, code=arguments)


def _info_block(event: InfoEvent) -> Block:
    body = (
        event.data
        if isinstance(event.data, str)
        else json.dumps(event.data, indent=2, sort_keys=True, ensure_ascii=False)
    )
    return Block("info", event.source or "info", body)
