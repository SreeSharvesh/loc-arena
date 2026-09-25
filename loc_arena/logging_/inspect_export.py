"""Write one run as a real Inspect ``.eval`` log with a span per agent; contract in docs/agent-log/spec.md."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from inspect_ai.event import Event as InspectEvent
from inspect_ai.event import InfoEvent, ModelEvent, ToolEvent
from inspect_ai.log import EvalConfig, EvalDataset, EvalLog, EvalSample, EvalSpec, write_eval_log

from loc_arena.config import RunConfig
from loc_arena.logging_.agent_trace import EpisodeTrace, ModelCall, TurnRef
from loc_arena.logging_.events import Event


class UnassignedEventError(RuntimeError):
    """A sealed event inside the episode boundary has no lane."""


@dataclass(frozen=True)
class EpisodeExport:
    """One episode to export as one Inspect sample."""

    sample_id: str
    trace: EpisodeTrace
    sealed_path: Path
    agent_order: tuple[str, ...]


def write_run_eval(
    path: Path,
    *,
    run_name: str,
    config: RunConfig,
    mode: str,
    seed: int,
    scores: Mapping[str, Any],
    episodes: Sequence[EpisodeExport],
) -> Path:
    log = EvalLog(
        status="success",
        eval=_eval_spec(run_name, config, mode, seed, len(episodes)),
        samples=[_sample(episode, scores if index == 0 else None) for index, episode in enumerate(episodes)],
    )
    write_eval_log(log, str(path))
    return path


def _eval_spec(run_name: str, config: RunConfig, mode: str, seed: int, sample_count: int) -> EvalSpec:
    agent_model = config.models.get("untrusted_agent")
    return EvalSpec(
        created=datetime.now(UTC).isoformat(),
        task=f"loc_arena/{config.run_slug}",
        task_id=config.run_slug,
        run_id=run_name,
        dataset=EvalDataset(name=config.run_slug, samples=sample_count),
        model=agent_model.model if agent_model is not None else "none/none",
        config=EvalConfig(),
        metadata={"mode": mode, "seed": seed, "policy": config.policy, "run_name": run_name},
    )


def _sample(episode: EpisodeExport, scores: Mapping[str, Any] | None) -> EvalSample:
    raise NotImplementedError


def _lanes_for(trace: EpisodeTrace, sealed_events: Sequence[Event]) -> dict[int, TurnRef | None]:
    raise NotImplementedError


def _sample_events(
    episode: EpisodeExport, sealed_events: Sequence[Event], lanes: Mapping[int, TurnRef | None]
) -> list[InspectEvent]:
    raise NotImplementedError


def _model_event(call: ModelCall, span_id: str | None) -> ModelEvent:
    raise NotImplementedError


def _tool_event(event: Event, span_id: str | None, at: datetime) -> ToolEvent:
    raise NotImplementedError


def _info_event(event: Event, span_id: str | None, at: datetime) -> InfoEvent:
    raise NotImplementedError


def _timestamp(wall: float) -> datetime:
    raise NotImplementedError
