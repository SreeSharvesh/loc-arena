"""In-memory per-agent trace of one episode; contract in docs/agent-log/spec.md."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from types import MappingProxyType

from loc_arena.logging_.events import Event
from loc_arena.stack.contracts import (
    EpisodeLanes,
    ModelCallRecord,
    ModelCallUsage,
    MonitorVerdictRecord,
    Phase,
    RunnerEpisodeExport,
    TurnReference,
    TurnTiming,
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
    """One provider call as the gateway core made it; its turn is the lane of ``sealed_seq``."""

    phase: Phase | None
    identity: str
    role: str
    model_input: str
    output: str
    sealed_seq: int
    wall_ts: float
    usage: ModelCallUsage


@dataclass(frozen=True)
class EpisodeTrace:
    """The finished, read-only trace of one episode."""

    turns: tuple[TurnRecord, ...]
    sealed_lane: Mapping[int, TurnRef | None]
    mirror_lane: Mapping[int, TurnRef | None]
    mirror_to_sealed: Mapping[int, int]
    phases: Mapping[int, Phase | None]
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
        self._phases: dict[int, Phase | None] = {}
        self._last_sealed: Event | None = None
        self._model_calls: list[ModelCall] = []

    @contextmanager
    def turn(self, agent_uid: str, turn: int) -> Iterator[None]:
        if self._bound is not None:
            raise RuntimeError(
                f"cannot bind {agent_uid} turn {turn}: "
                f"{self._bound.agent_uid} turn {self._bound.turn} is still running",
            )
        ref = TurnRef(agent_uid, turn)
        started = self._wall_clock()
        self._bound, self._phase = ref, "deciding"
        try:
            yield
        finally:
            self._bound, self._phase = None, None
            self._turns.append(TurnRecord(ref, started, self._wall_clock()))

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
            self._last_sealed = None

    def on_model_reply(self, sealed_seqs: Iterable[int], mirror_seqs: Iterable[int]) -> None:
        for seq in sealed_seqs:
            self._sealed_lane[seq] = self._bound
            self._phases[seq] = self._phase
        for seq in mirror_seqs:
            self._mirror_lane[seq] = self._bound

    def on_model_call(
        self,
        *,
        identity: str,
        role: str,
        model_input: str,
        output: str,
        sealed_seq: int,
        usage: ModelCallUsage,
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
                usage=usage,
            ),
        )

    def on_model_call_failed(self, *, sealed_seq: int) -> None:
        """A call the provider failed: the core's sealed record of it is the bound turn's, as a reply's is."""
        self.on_model_reply((sealed_seq,), ())

    def finish(self, last_sealed_seq: int) -> EpisodeTrace:
        if self._bound is not None:
            raise RuntimeError(
                f"episode ended while {self._bound.agent_uid} turn {self._bound.turn} was still bound",
            )
        return EpisodeTrace(
            turns=tuple(self._turns),
            sealed_lane=MappingProxyType(dict(self._sealed_lane)),
            mirror_lane=MappingProxyType(dict(self._mirror_lane)),
            mirror_to_sealed=MappingProxyType(dict(self._mirror_to_sealed)),
            phases=MappingProxyType(dict(self._phases)),
            model_calls=tuple(self._model_calls),
            last_sealed_seq=last_sealed_seq,
        )


def _same_logical_event(sealed: Event, mirror: Event) -> bool:
    return replace(sealed, seq=0, fp="") == replace(mirror, seq=0, fp="")


def export_runner_episode(
    trace: EpisodeTrace | None,
    *,
    handle: str,
    episode_id: str,
    deadline_ts: float,
    last_sealed_seq: int,
    verdicts: Sequence[MonitorVerdictRecord],
) -> RunnerEpisodeExport:
    return RunnerEpisodeExport(
        handle=handle,
        episode_id=episode_id,
        deadline_ts=deadline_ts,
        last_sealed_seq=last_sealed_seq,
        verdicts=tuple(verdicts),
        turns=() if trace is None else tuple(_timing(record) for record in trace.turns),
        lanes=EpisodeLanes(
            sealed={} if trace is None else _references(trace.sealed_lane),
            mirror={} if trace is None else _references(trace.mirror_lane),
        ),
        phases={} if trace is None else dict(trace.phases),
        mirror_to_sealed={} if trace is None else dict(trace.mirror_to_sealed),
    )


def merge_runner_episode(
    export: RunnerEpisodeExport,
    model_calls: Sequence[ModelCallRecord],
    sealed_seqs: Iterable[int],
) -> EpisodeTrace:
    known = {seq: _turn_ref(reference) for seq, reference in export.lanes.sealed.items()}
    return EpisodeTrace(
        turns=tuple(
            TurnRecord(TurnRef(timing.agent_uid, timing.turn), timing.wall_start, timing.wall_end)
            for timing in export.turns
        ),
        sealed_lane=MappingProxyType(_fill_lanes(known, sealed_seqs, export.last_sealed_seq)),
        mirror_lane=MappingProxyType(
            {seq: _turn_ref(reference) for seq, reference in export.lanes.mirror.items()},
        ),
        mirror_to_sealed=MappingProxyType(dict(export.mirror_to_sealed)),
        phases=MappingProxyType(dict(export.phases)),
        model_calls=tuple(
            ModelCall(
                phase=export.phases.get(record.sealed_seq),
                identity=record.identity,
                role=record.role,
                model_input=record.model_input,
                output=record.output,
                sealed_seq=record.sealed_seq,
                wall_ts=record.wall_ts,
                usage=record.usage,
            )
            for record in model_calls
        ),
        last_sealed_seq=export.last_sealed_seq,
    )


def _fill_lanes(
    known: Mapping[int, TurnRef | None],
    sealed_seqs: Iterable[int],
    last_sealed_seq: int,
) -> dict[int, TurnRef | None]:
    in_episode = sorted(seq for seq in set(sealed_seqs) | set(known) if seq <= last_sealed_seq)
    lanes: dict[int, TurnRef | None] = {}
    before: TurnRef | None = None
    unknown: list[int] = []
    for seq in in_episode:
        if seq not in known:
            unknown.append(seq)
            continue
        lane = known[seq]
        for gap in unknown:  # the seqs since the previous known one: inside a turn only if it holds both
            lanes[gap] = lane if lane is not None and lane == before else None
        unknown.clear()
        lanes[seq] = before = lane
    lanes.update(dict.fromkeys(unknown))  # after the last known seq: World
    return lanes


def _timing(record: TurnRecord) -> TurnTiming:
    return TurnTiming(
        agent_uid=record.ref.agent_uid,
        turn=record.ref.turn,
        wall_start=record.wall_start,
        wall_end=record.wall_end,
    )


def _references(lanes: Mapping[int, TurnRef | None]) -> dict[int, TurnReference | None]:
    return {
        seq: None if ref is None else TurnReference(agent_uid=ref.agent_uid, turn=ref.turn)
        for seq, ref in lanes.items()
    }


def _turn_ref(reference: TurnReference | None) -> TurnRef | None:
    return None if reference is None else TurnRef(reference.agent_uid, reference.turn)
