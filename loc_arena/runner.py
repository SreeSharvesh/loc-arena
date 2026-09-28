"""The runner's side of one finished episode, wherever the services run: close it, monitor it, export it.

Once the scaffold's policy returns, ``finish_episode`` closes the episode on the core (its model routes stop
serving it, so nothing agent code left running is recorded under it), finishes the agents' trace at the
last sealed seq, and runs the protocol's monitors over the mirror. A monitor's own model call goes through
the core (``call_model_as_monitor``): a sealed, attributable record. A call that fails is recorded as that
monitor's ``monitor_error`` on the sealed log and the monitor falls back to its heuristic, so the episode
still completes. The verdicts then travel to the host in the ``RunnerEpisodeExport``.
"""

from __future__ import annotations

from collections.abc import Iterable
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
from loc_arena.stack.contracts import EpisodeWiring, MonitorVerdictRecord

_EVENT_KIND: Final = TypeAdapter(EventKind)
MONITOR_ACTOR_ROLE: Final = "trusted"  # the role a monitor's own sealed records carry


@dataclass(frozen=True)
class EpisodeMonitoring:
    """How the runner scores an episode's mirror: the protocol's monitors, and whether they call models.

    With ``calls_models``, a live monitor's model calls go through the core; without, every monitor uses its
    heuristic and writes nothing.
    """

    protocol: Protocol
    calls_models: bool


@dataclass(frozen=True)
class RunnerEpisode:
    """What the runner holds of a finished episode: its last sealed seq, the agents' trace, the verdicts."""

    last_sealed_seq: int
    trace: EpisodeTrace | None
    verdicts: tuple[MonitorVerdict, ...]


class MonitorModelRoute:
    """A live monitor's route to a model through the core, and to the sealed log for what went wrong."""

    def __init__(self, wiring: EpisodeWiring, clock: SimulatedClock) -> None:
        """Call models through ``wiring``'s core; stamp the monitors' sealed records with ``clock``."""
        self._wiring = wiring
        self._clock = clock

    def call_model(self, monitor: str, prompt: str, temperature: float) -> str:
        """The monitor's completion; a failed call is recorded as its ``monitor_error``, then raised."""
        try:
            return self._wiring.control.call_model_as_monitor(monitor, prompt, temperature)
        except GATEWAY_FAILURES as error:
            self.record_event("monitor_error", {"monitor": monitor, "error": f"model call failed: {error}"})
            raise GatewayCallError(f"monitor {monitor}'s model call failed: {error}") from error

    def record_event(self, kind: str, payload: dict[str, Any]) -> None:
        """Record a monitor's event (an error, an unparseable verdict) on the sealed log."""
        draft = EventDraft(
            ts=self._clock.now,
            actor_uid=str(payload.get("monitor", "monitor")),
            actor_role=MONITOR_ACTOR_ROLE,
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
        route = MonitorModelRoute(wiring, clock)
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


def export_verdicts(verdicts: Iterable[MonitorVerdict]) -> tuple[MonitorVerdictRecord, ...]:
    """The verdicts as the runner's export carries them."""
    return tuple(MonitorVerdictRecord.model_validate(verdict, from_attributes=True) for verdict in verdicts)


def import_verdicts(records: Iterable[MonitorVerdictRecord]) -> tuple[MonitorVerdict, ...]:
    """The verdicts of a runner's export, as the host scores them."""
    return tuple(MonitorVerdict(**record.model_dump()) for record in records)
