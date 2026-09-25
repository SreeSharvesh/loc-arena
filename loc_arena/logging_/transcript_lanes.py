"""Inspect sample -> per-agent lanes of blocks, one row per round; contract in spec.md beside this module."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from inspect_ai.event import Event as InspectEvent
from inspect_ai.event import InfoEvent, ModelEvent, SpanBeginEvent, ToolEvent
from inspect_ai.log import EvalSample, resolve_sample_attachments

WORLD = "World"
BEFORE_FIRST_ROUND = -1

BlockKind = Literal["prompt", "reply", "tool", "info"]


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
    return SampleTranscript(
        sample_id=str(sample.id),
        lanes=_lane_order(sample.metadata.get("agents", []), seen),
        rows=tuple(sorted({row for _, row in cells})),
        cells=MappingProxyType({key: tuple(blocks) for key, blocks in cells.items()}),
    )


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


def _lane_order(configured: Sequence[str], seen: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys([WORLD, *configured, *seen]))


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
