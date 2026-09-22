"""Main-task registration for the Aurora scenario: registers the continuous cost scorer by name."""

from __future__ import annotations

from pathlib import Path

from loc_arena.config import RunConfig
from loc_arena.registry import register_scorer
from loc_arena.tasks.main_task_grader import MainTaskScore, tamperproof_grade


@register_scorer("aurora_pipeline_cost")
def score(checkout: Path, config: RunConfig, reference_dir: Path) -> MainTaskScore:
    """Grade the Aurora pipeline with the sealed, tamper-proof cost meter (see main_task_grader)."""
    return tamperproof_grade(checkout, reference_dir, config)
