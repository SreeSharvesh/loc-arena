"""Main-task registration for the Aurora scenario: the continuous pipeline-cost and sweep-cost scorers.

Importing this module registers both scorers by name; a run config's ``main_task.scorer`` picks one.
``aurora_pipeline_cost`` grades the inference-and-evaluation pipeline (``aurora-efficiency.yaml``);
``aurora_sweep_cost`` grades the early-stopping hyperparameter sweep (``aurora-sweep.yaml``).
"""

from __future__ import annotations

from pathlib import Path

from loc_arena.config import RunConfig
from loc_arena.registry import register_scorer
from loc_arena.tasks.main_task_grader import MainTaskScore, tamperproof_grade
from loc_arena.tasks.sweep_grader import sweep_grade


@register_scorer("aurora_pipeline_cost")
def score(checkout: Path, config: RunConfig, reference_dir: Path) -> MainTaskScore:
    """Grade the Aurora pipeline with the sealed, tamper-proof cost meter (see main_task_grader)."""
    return tamperproof_grade(checkout, reference_dir, config)


@register_scorer("aurora_sweep_cost")
def score_sweep(checkout: Path, config: RunConfig, reference_dir: Path) -> MainTaskScore:
    """Grade the early-stopping sweep with the sealed job-step meter and winner gate (see sweep_grader)."""
    return sweep_grade(checkout, reference_dir, config)
