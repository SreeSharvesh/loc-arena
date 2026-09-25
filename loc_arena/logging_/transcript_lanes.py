"""Inspect sample -> per-agent lanes of blocks, one row per round; contract in docs/agent-log/spec.md."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    raise NotImplementedError


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
