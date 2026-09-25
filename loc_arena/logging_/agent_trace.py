"""In-memory per-agent trace of one episode; contract in docs/agent-log/spec.md."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from loc_arena.logging_.events import Event

Phase = Literal["deciding", "executing"]

_DUAL_RECORDED_FIELDS = (
    "kind",
    "actor_uid",
    "actor_role",
    "target_id",
    "turn",
    "tool",
    "ts",
    "payload",
    "result",
)


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
        self._wall_clock = wall_clock
        self._bound: TurnRef | None = None
        self._phase: Phase | None = None
        self._turns: list[TurnRecord] = []
        self._sealed_lane: dict[int, TurnRef | None] = {}
        self._mirror_lane: dict[int, TurnRef | None] = {}
        self._mirror_to_sealed: dict[int, int] = {}
        self._last_sealed: Event | None = None
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
        self._bound, self._phase = ref, "deciding"
        try:
            yield
        finally:
            self._turns.append(TurnRecord(ref, started, self._wall_clock()))
            self._bound, self._phase = None, None

    def mark_executing(self) -> None:
        if self._bound is None:
            raise RuntimeError("mark_executing called with no turn bound")
        self._phase = "executing"

    def on_sealed_append(self, event: Event) -> None:
        self._sealed_lane[event.seq] = self._bound
        self._last_sealed = event

    def on_mirror_append(self, event: Event) -> None:
        self._mirror_lane[event.seq] = self._bound
        twin = self._last_sealed
        if twin is not None and _same_logical_event(twin, event):
            self._mirror_to_sealed[event.seq] = twin.seq

    def on_model_call(
        self, *, identity: str, role: str, model_input: str, output: str, sealed_seq: int
    ) -> None:
        self._model_calls.append(
            ModelCall(
                ref=self._bound,
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
            mirror_lane=MappingProxyType(dict(self._mirror_lane)),
            mirror_to_sealed=MappingProxyType(dict(self._mirror_to_sealed)),
            model_calls=tuple(self._model_calls),
            last_sealed_seq=last_sealed_seq,
        )


def _same_logical_event(sealed: Event, mirror: Event) -> bool:
    return all(getattr(sealed, name) == getattr(mirror, name) for name in _DUAL_RECORDED_FIELDS)
