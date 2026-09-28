"""What a run's model calls consumed and how many failed, per model role, read from each episode's sealed log.

The gateway core seals every provider call it completes as one ``inference_call`` event carrying the call's
``model_role`` and, flat beside it, its ``prompt_tokens``, ``completion_tokens``, ``cached_tokens`` and
``cost``, and every call the provider fails as one ``inference_error`` event carrying its ``model_role`` and
``failure`` (the provider's status and message). ``summarize_model_usage`` totals them per role
(``untrusted_agent``, ``trusted_monitor``, ``teacher``) and over all roles, and groups the failures by reason;
``scores.json`` carries a run's totals under ``model_usage``, and ``report.html`` shows them.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from loc_arena.logging_.events import Event, read_events
from loc_arena.stack.contracts import ModelCallFailure, ModelCallUsage


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


class SealedModelCallFailure(BaseModel):
    """The fields of a sealed ``inference_error`` payload that the totals read, validated on reading."""

    model_config = ConfigDict(frozen=True)

    model_role: str
    failure: ModelCallFailure


@dataclass(frozen=True)
class UsageTotal:
    """How many calls completed and failed, and what the completed ones consumed.

    ``cached_tokens`` is how many of the ``prompt_tokens`` came from a cached prefix. ``cost`` is in
    OpenRouter credits (US dollars), ``None`` when a completed call reported no cost, so the total is
    unknown.
    """

    completed_calls: int
    failed_calls: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    total_tokens: int
    cost: float | None


@dataclass(frozen=True)
class FailureReason:
    """One reason the provider gave for failing calls, and how many calls it failed for it."""

    status_code: int | None
    message: str
    failed_calls: int


@dataclass(frozen=True)
class EpisodeModelUsage:
    """One episode's model usage, per model role and over all roles.

    ``failure_reasons`` says why calls failed, the most frequent reason first.
    """

    by_role: dict[str, UsageTotal]
    total: UsageTotal
    failure_reasons: tuple[FailureReason, ...]


@dataclass(frozen=True)
class RunModelUsage:
    """A run's model usage: its episode's, and its honest calibration twin's when that is a second episode.

    ``calibration`` is ``None`` in honest mode, where the episode is its own calibration twin.
    """

    episode: EpisodeModelUsage
    calibration: EpisodeModelUsage | None


def summarize_model_usage(events: Iterable[Event]) -> EpisodeModelUsage:
    """Total the completed and failed model calls among an episode's sealed ``events``, per model role."""
    calls: list[SealedModelCall] = []
    failures: list[SealedModelCallFailure] = []
    for event in events:
        if event.kind == "inference_call":
            calls.append(SealedModelCall.model_validate(event.payload))
        elif event.kind == "inference_error":
            failures.append(SealedModelCallFailure.model_validate(event.payload))
    roles = sorted({call.model_role for call in calls} | {failed.model_role for failed in failures})
    reasons = Counter(failed.failure for failed in failures).most_common()
    return EpisodeModelUsage(
        by_role={role: _total_role(role, calls, failures) for role in roles},
        total=total_usage([call.usage for call in calls], failed_calls=len(failures)),
        failure_reasons=tuple(
            FailureReason(status_code=failure.status_code, message=failure.message, failed_calls=count)
            for failure, count in reasons
        ),
    )


def _total_role(
    role: str,
    calls: Sequence[SealedModelCall],
    failures: Sequence[SealedModelCallFailure],
) -> UsageTotal:
    usages = [call.usage for call in calls if call.model_role == role]
    return total_usage(usages, failed_calls=sum(failed.model_role == role for failed in failures))


def read_run_model_usage(episode_log: Path, calibration_log: Path | None) -> RunModelUsage:
    """The usage of a run: its episode's, sealed at ``episode_log``, and its twin's, at ``calibration_log``.

    ``calibration_log`` is ``None`` when the episode is its own calibration twin (honest mode).
    """
    calibration = None if calibration_log is None else summarize_model_usage(read_events(calibration_log))
    return RunModelUsage(episode=summarize_model_usage(read_events(episode_log)), calibration=calibration)


def total_usage(usages: Sequence[ModelCallUsage], *, failed_calls: int) -> UsageTotal:
    """The sum of ``usages``, beside ``failed_calls``; the cost is unknown (``None``) if one reported none."""
    prompt_tokens = sum(usage.prompt_tokens for usage in usages)
    completion_tokens = sum(usage.completion_tokens for usage in usages)
    reported_costs = [usage.cost for usage in usages if usage.cost is not None]
    return UsageTotal(
        completed_calls=len(usages),
        failed_calls=failed_calls,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=sum(usage.cached_tokens for usage in usages),
        total_tokens=prompt_tokens + completion_tokens,
        cost=math.fsum(reported_costs) if len(reported_costs) == len(usages) else None,
    )
