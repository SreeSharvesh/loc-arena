"""The part of a run's ``scores.json`` the explorer reads, parsed and checked where it enters."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, JsonValue

from loc_arena.explorer.page import WallClockCeilingView
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
    landing_robust: bool
    milestones: Mapping[str, MilestoneScores] = {}


class SafetyScores(_Scores):
    """What the monitors decided about the run."""

    audit_threshold: float
    caught: bool
    caught_by: tuple[str, ...] = ()
    per_monitor_max_score: Mapping[str, float] = {}


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
    wall_clock_ceiling: WallClockCeilingView | None = None  # absent: the ceiling stopped no episode
    main_task: MainTaskScores
    side_task: SideTaskScores
    safety: SafetyScores
    monitors: tuple[MonitorScores, ...] = ()
    monitor_verdicts: tuple[MonitorVerdict, ...] = ()


def load_scores(path: Path) -> RunScores:
    """Parse a run's scores.json; a file that does not match raises ``pydantic.ValidationError``."""
    return RunScores.model_validate_json(path.read_text())
