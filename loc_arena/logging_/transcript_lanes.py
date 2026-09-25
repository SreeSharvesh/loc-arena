"""Inspect sample -> per-agent lanes of blocks, one row per round; contract in docs/agent-log/spec.md."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from inspect_ai.event import Event as InspectEvent
from inspect_ai.event import InfoEvent, ModelEvent, ToolEvent
from inspect_ai.log import EvalSample

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
    owners = _turn_owners(sample.events)
    cells: dict[tuple[str, int], list[Block]] = {}
    seen: list[str] = []
    latest_round = BEFORE_FIRST_ROUND
    for event in sample.events:
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
    raise NotImplementedError


def _lane_order(configured: Sequence[str], seen: Sequence[str]) -> tuple[str, ...]:
    raise NotImplementedError


def _blocks(event: InspectEvent) -> tuple[Block, ...]:
    raise NotImplementedError


def _model_blocks(event: ModelEvent) -> tuple[Block, ...]:
    raise NotImplementedError


def _tool_block(event: ToolEvent) -> Block:
    raise NotImplementedError


def _info_block(event: InfoEvent) -> Block:
    raise NotImplementedError
