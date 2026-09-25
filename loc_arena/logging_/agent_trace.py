"""In-memory per-agent trace of one episode; contract in docs/agent-log/spec.md."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Literal

from loc_arena.logging_.events import Event

Phase = Literal["deciding", "executing"]


@dataclass(frozen=True)
class TurnRef:
    """One agent turn."""

    agent_uid: str
    turn: int


@dataclass(frozen=True)
class TurnRecord:
    """Wall-clock bounds of one completed turn."""

    ref: TurnRef
    wall_start: float
    wall_end: float


@dataclass(frozen=True)
class ModelCall:
    """One provider call as the gateway core made it."""

    ref: TurnRef | None
    phase: Phase | None
    identity: str
    role: str
    model_input: str
    output: str
    sealed_seq: int
    wall_ts: float


@dataclass(frozen=True)
class EpisodeTrace:
    """The finished, read-only trace of one episode."""

    turns: tuple[TurnRecord, ...]
    sealed_lane: Mapping[int, TurnRef | None]
    mirror_lane: Mapping[int, TurnRef | None]
    mirror_to_sealed: Mapping[int, int]
    model_calls: tuple[ModelCall, ...]
    last_sealed_seq: int


class AgentTrace:
    """Collects one episode's per-turn attribution from scaffold, recorder and core hooks."""

    def __init__(self, *, wall_clock: Callable[[], float] = time.time) -> None:
        raise NotImplementedError

    def turn(self, agent_uid: str, turn: int) -> AbstractContextManager[None]:
        raise NotImplementedError

    def mark_executing(self) -> None:
        raise NotImplementedError

    def on_sealed_append(self, event: Event) -> None:
        raise NotImplementedError

    def on_mirror_append(self, event: Event) -> None:
        raise NotImplementedError

    def on_model_call(
        self, *, identity: str, role: str, model_input: str, output: str, sealed_seq: int
    ) -> None:
        raise NotImplementedError

    def finish(self, last_sealed_seq: int) -> EpisodeTrace:
        raise NotImplementedError
