"""In-memory per-agent trace of one episode; contract in spec.md beside this module."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from loc_arena.logging_.events import AppendOnlyLog, Event

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
    executing_from_seq: int | None = None


@dataclass(frozen=True)
class ModelCall:
    """One provider call as the gateway core made it; its turn is the lane of ``sealed_seq``."""

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
    model_calls: tuple[ModelCall, ...]
    last_sealed_seq: int


class AgentTrace:
    """Collects one episode's per-turn attribution from scaffold, recorder and core hooks."""

    def __init__(self, *, wall_clock: Callable[[], float] = time.time) -> None:
        self._wall_clock = wall_clock
        self._bound: TurnRef | None = None
        self._phase: Phase | None = None
        self._turns: list[TurnRecord] = []
        self._sealed_lane: dict[int, TurnRef | None] = {}
        self._next_sealed_seq = 0
        self._executing_from_seq: int | None = None
        self._model_calls: list[ModelCall] = []

    @contextmanager
    def turn(self, agent_uid: str, turn: int) -> Iterator[None]:
        if self._bound is not None:
            raise RuntimeError(
                f"cannot bind {agent_uid} turn {turn}: "
                f"{self._bound.agent_uid} turn {self._bound.turn} is still running"
            )
        ref = TurnRef(agent_uid, turn)
        started = self._wall_clock()
        self._bound, self._phase, self._executing_from_seq = ref, "deciding", None
        try:
            yield
        finally:
            executing_from_seq = self._executing_from_seq
            self._bound, self._phase, self._executing_from_seq = None, None, None
            self._turns.append(TurnRecord(ref, started, self._wall_clock(), executing_from_seq))

    def mark_executing(self) -> None:
        if self._bound is None:
            raise RuntimeError("mark_executing called with no turn bound")
        self._phase = "executing"
        self._executing_from_seq = self._next_sealed_seq

    def on_sealed_append(self, event: Event) -> None:
        self._sealed_lane[event.seq] = self._bound
        self._next_sealed_seq = event.seq + 1

    def on_model_call(
        self, *, identity: str, role: str, model_input: str, output: str, sealed_seq: int
    ) -> None:
        self._model_calls.append(
            ModelCall(
                phase=self._phase,
                identity=identity,
                role=role,
                model_input=model_input,
                output=output,
                sealed_seq=sealed_seq,
                wall_ts=self._wall_clock(),
            )
        )

    def finish(self, last_sealed_seq: int) -> EpisodeTrace:
        if self._bound is not None:
            raise RuntimeError(
                f"episode ended while {self._bound.agent_uid} turn {self._bound.turn} was still bound"
            )
        return EpisodeTrace(
            turns=tuple(self._turns),
            sealed_lane=MappingProxyType(dict(self._sealed_lane)),
            model_calls=tuple(self._model_calls),
            last_sealed_seq=last_sealed_seq,
        )


def open_episode_logs(
    sealed_path: Path, mirror_path: Path, episode_id: str, *, traced: bool
) -> tuple[AgentTrace | None, AppendOnlyLog, AppendOnlyLog]:
    trace = AgentTrace() if traced else None
    sealed = AppendOnlyLog(sealed_path, episode_id, on_append=trace.on_sealed_append if trace else None)
    mirror = AppendOnlyLog(mirror_path, episode_id)
    return trace, sealed, mirror
