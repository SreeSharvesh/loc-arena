"""What a run's model calls consumed, per model role, read from each episode's sealed log.

The gateway core seals every provider call it completes as one ``inference_call`` event carrying the call's
``model_role`` and, flat beside it, its ``prompt_tokens``, ``completion_tokens``, ``cached_tokens`` and
``cost``. ``summarize_model_usage`` totals them per role
(``untrusted_agent``, ``trusted_monitor``, ``teacher``) and over all roles; ``scores.json`` carries a run's
totals under ``model_usage``, and ``report.html`` shows them.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from loc_arena.logging_.events import Event, read_events
from loc_arena.stack.contracts import ModelCallUsage


class SealedModelCall(BaseModel):
    """The fields of a sealed ``inference_call`` payload that the totals read, validated on reading.

    The payload's other fields (the stamps the verifier reads) are ignored here.
    """

    model_config = ConfigDict(frozen=True)

    model_role: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost: float | None

    @property
    def usage(self) -> ModelCallUsage:
        """The call's token counts and cost."""
        return ModelCallUsage.model_validate(self, from_attributes=True)


@dataclass(frozen=True)
class UsageTotal:
    """What a set of completed calls consumed.

    ``cached_tokens`` is how many of the ``prompt_tokens`` came from a cached prefix. ``cost`` is in
    OpenRouter credits (US dollars), ``None`` when a call among them reported no cost, so the total is
    unknown.
    """

    completed_calls: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    total_tokens: int
    cost: float | None


@dataclass(frozen=True)
class EpisodeModelUsage:
    """One episode's model usage: per model role, and over all roles."""

    by_role: dict[str, UsageTotal]
    total: UsageTotal


@dataclass(frozen=True)
class RunModelUsage:
    """A run's model usage: its episode's, and its honest calibration twin's when that is a second episode.

    ``calibration`` is ``None`` in honest mode, where the episode is its own calibration twin.
    """

    episode: EpisodeModelUsage
    calibration: EpisodeModelUsage | None


def summarize_model_usage(events: Iterable[Event]) -> EpisodeModelUsage:
    """Total the usage of the completed model calls among an episode's sealed ``events``, per model role."""
    calls = [
        SealedModelCall.model_validate(event.payload) for event in events if event.kind == "inference_call"
    ]
    usages_by_role: dict[str, list[ModelCallUsage]] = {}
    for call in sorted(calls, key=lambda call: call.model_role):
        usages_by_role.setdefault(call.model_role, []).append(call.usage)
    return EpisodeModelUsage(
        by_role={role: total_usage(usages) for role, usages in usages_by_role.items()},
        total=total_usage([call.usage for call in calls]),
    )


def read_run_model_usage(episode_log: Path, calibration_log: Path | None) -> RunModelUsage:
    """The usage of a run: its episode's, sealed at ``episode_log``, and its twin's, at ``calibration_log``.

    ``calibration_log`` is ``None`` when the episode is its own calibration twin (honest mode).
    """
    calibration = None if calibration_log is None else summarize_model_usage(read_events(calibration_log))
    return RunModelUsage(episode=summarize_model_usage(read_events(episode_log)), calibration=calibration)


def total_usage(usages: Sequence[ModelCallUsage]) -> UsageTotal:
    """The sum of ``usages``; its cost is unknown (``None``) as soon as one of them reported none."""
    prompt_tokens = sum(usage.prompt_tokens for usage in usages)
    completion_tokens = sum(usage.completion_tokens for usage in usages)
    reported_costs = [usage.cost for usage in usages if usage.cost is not None]
    return UsageTotal(
        completed_calls=len(usages),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=sum(usage.cached_tokens for usage in usages),
        total_tokens=prompt_tokens + completion_tokens,
        cost=math.fsum(reported_costs) if len(reported_costs) == len(usages) else None,
    )
