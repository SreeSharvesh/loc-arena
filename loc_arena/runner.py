"""The runner's side of one finished episode, wherever the services run: close it, monitor it."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from pydantic import TypeAdapter

from loc_arena.config import RunConfig
from loc_arena.evidence import read_event_log
from loc_arena.gateway.client import GATEWAY_FAILURES, GatewayCallError
from loc_arena.logging_.agent_trace import AgentTrace, EpisodeTrace
from loc_arena.logging_.events import EventDraft, EventKind
from loc_arena.metrics import score_mirror
from loc_arena.monitors.registry import MonitorVerdict
from loc_arena.protocol import Protocol
from loc_arena.scaffold.clock import SimulatedClock
from loc_arena.stack.contracts import EpisodeWiring

_EVENT_KIND: Final = TypeAdapter(EventKind)


@dataclass(frozen=True)
class EpisodeMonitoring:
    """How the runner scores an episode's mirror: the protocol's monitors, and whether they call models."""

    protocol: Protocol
    calls_models: bool


@dataclass(frozen=True)
class RunnerEpisode:
    """What the runner holds of a finished episode: its last sealed seq, the agents' trace, the verdicts."""

    last_sealed_seq: int
    trace: EpisodeTrace | None
    verdicts: tuple[MonitorVerdict, ...]


class MonitorModelRoute:
    """A live monitor's route to a model through the core, and to the sealed log for what went wrong.

    A failed call has already spent the provider's whole call budget, so once a monitor's calls have failed
    ``failures_before_fallback`` times in a row it calls its model no more: its later calls fail at once, and
    it scores the rest of the episode with its heuristic. A completed call starts the count again.
    """

    def __init__(
        self,
        wiring: EpisodeWiring,
        clock: SimulatedClock,
        *,
        failures_before_fallback: int,
    ) -> None:
        """Call models through ``wiring``'s core; stamp the monitors' sealed records with ``clock``."""
        self._wiring = wiring
        self._clock = clock
        self._failures_before_fallback = failures_before_fallback
        self._failures_in_a_row: Counter[str] = Counter()  # each monitor runs on one thread: one key each

    def call_model(self, monitor: str, prompt: str, temperature: float) -> str:
        """The monitor's completion; a failed call is recorded as its ``monitor_error``, then raised."""
        if self._failures_in_a_row[monitor] >= self._failures_before_fallback:
            raise GatewayCallError(
                f"monitor {monitor} calls its model no more: its last {self._failures_before_fallback} "
                "calls failed",
            )
        try:
            completion = self._wiring.control.call_model_as_monitor(monitor, prompt, temperature)
        except GATEWAY_FAILURES as error:
            self._failures_in_a_row[monitor] += 1
            self.record_event("monitor_error", {"monitor": monitor, "error": f"model call failed: {error}"})
            raise GatewayCallError(f"monitor {monitor}'s model call failed: {error}") from error
        self._failures_in_a_row[monitor] = 0
        return completion

    def record_event(self, kind: str, payload: dict[str, Any]) -> None:
        """Record a monitor's event (an error, an unparseable verdict) on the sealed log."""
        draft = EventDraft(
            ts=self._clock(),
            actor_uid=str(payload.get("monitor", "monitor")),
            actor_role="trusted",
            kind=_EVENT_KIND.validate_python(kind),
            payload=payload,
        )
        self._wiring.sealed.record(draft)


def finish_episode(
    config: RunConfig,
    wiring: EpisodeWiring,
    clock: SimulatedClock,
    trace: AgentTrace | None,
    *,
    mirror_path: Path,
    monitoring: EpisodeMonitoring | None,
) -> RunnerEpisode:
    """Close the episode, finish its trace, and score its mirror at ``mirror_path`` (no monitoring: none)."""
    last_sealed_seq = wiring.control.close()
    episode_trace = None if trace is None else trace.finish(last_sealed_seq)
    if monitoring is None:
        return RunnerEpisode(last_sealed_seq, episode_trace, ())
    mirror_events = read_event_log(mirror_path, config.settings.docker.evidence_max_bytes)
    if monitoring.calls_models:
        route = MonitorModelRoute(
            wiring,
            clock,
            failures_before_fallback=config.settings.gateway.monitor_failures_before_fallback,
        )
        verdicts = score_mirror(
            mirror_events,
            config,
            monitoring.protocol,
            model_caller=route.call_model,
            event_sink=route.record_event,
        )
    else:
        verdicts = score_mirror(mirror_events, config, monitoring.protocol)
    return RunnerEpisode(last_sealed_seq, episode_trace, tuple(verdicts))
