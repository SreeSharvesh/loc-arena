"""Main-task registration for the Aurora scenario: the continuous cost scorer, under its name.

The measure step (``measure.py``) runs the suites and the sealed cost meter where the checkout is: the
networkless grader container in the stack, this machine for STACK=0. The score step below reads that
measurement on the host against the sealed ``reference.json`` (``main_task_grader.score_measurement``).
"""

from __future__ import annotations

from pathlib import Path

from loc_arena.config import RunConfig
from loc_arena.registry import ScorerSteps, register_scorer
from loc_arena.stack.contracts import GradeMeasurement
from loc_arena.tasks.main_task_grader import MainTaskScore, load_grade_reference, score_measurement
from scenarios.aurora_efficiency.measure import MEASURE_STEP


def score(measurement: GradeMeasurement, config: RunConfig, reference_directory: Path) -> MainTaskScore:
    """Score the measured cost against the sealed reference, gated on correctness (see main_task_grader)."""
    return score_measurement(measurement, load_grade_reference(reference_directory), config)


register_scorer(ScorerSteps(measure_step=MEASURE_STEP, score=score))
