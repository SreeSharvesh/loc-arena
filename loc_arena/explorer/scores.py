"""The part of a run's ``scores.json`` the explorer reads, parsed and checked where it enters."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, JsonValue

from loc_arena.monitors.registry import MonitorVerdict


class _Scores(BaseModel):
    """A frozen slice of scores.json; fields the explorer does not read are ignored."""

    model_config = ConfigDict(frozen=True)


class MainTaskScores(_Scores):
    """How the assigned task went."""

    final_score: float
    gate_passed: bool
    gate_reason: str | None = None


class MilestoneScores(_Scores):
    """One side-task milestone as the sealed verifier found it, with the evidence it cites."""

    reached: bool
    sealed: bool
    evidence: Mapping[str, JsonValue] = {}


class SideTaskScores(_Scores):
    """How far the side task got."""

    landing_minimal: bool
    milestones: Mapping[str, MilestoneScores] = {}


class AggregationScores(_Scores):
    """How the monitors' scores combined into the caught decision."""

    threshold: float
    per_monitor: Mapping[str, float] = {}


class SafetyScores(_Scores):
    """What the monitors decided about the run."""

    audit_threshold: float
    caught: bool
    caught_by: tuple[str, ...] = ()
    per_monitor_max_score: Mapping[str, float] = {}
    overall_suspicion: float | None = None
    aggregation: AggregationScores | None = None


class HonestTwinScores(_Scores):
    """An attack run's ``honest_twin`` block: what the monitors made of the play the thresholds come from."""

    overall_suspicion: float
    per_monitor: Mapping[str, float] = {}
    monitor_verdicts: tuple[MonitorVerdict, ...] = ()


class MonitorScores(_Scores):
    """One deployed monitor or detector, as the run's scores describe it."""

    name: str
    role: str
    mode: str
    timing: str
    counts_toward_caught: bool
    errored: bool = False
    top_reasons: tuple[str, ...] = ()


class RunScores(_Scores):
    """A run's scores.json: the outcome, the monitors, and every verdict they gave."""

    run_slug: str
    mode: str
    seed: int
    models: Mapping[str, str]
    wall_clock_seconds: float
    main_task: MainTaskScores
    side_task: SideTaskScores
    safety: SafetyScores
    monitors: tuple[MonitorScores, ...] = ()
    monitor_verdicts: tuple[MonitorVerdict, ...] = ()
    honest_twin: HonestTwinScores | None = None  # none in an honest run, and before grading kept it


def load_scores(path: Path) -> RunScores:
    """Parse a run's scores.json; a file that does not match raises ``pydantic.ValidationError``."""
    return RunScores.model_validate_json(path.read_text())
