"""The Aurora scorer's measure step: every repository's suite, then the sealed cost benchmark.

Runs where the checkout is: in the stack, the grader container loads this file from a read-only bind mount
(``loc_arena.grader.measure_steps.load_measure_module``), so it imports only what the sandbox image ships. The
host scores its ``GradeMeasurement`` with ``main.py``'s score step against ``reference/reference.json``.
"""

from __future__ import annotations

from typing import Final

from loc_arena.grader.measure_steps import MeasureStep, register_measure_step
from loc_arena.grader.measurement import INCOMPLETE_MEASUREMENT, measure_checkout
from loc_arena.stack.contracts import GradeMeasurement

MEASURE_STEP: Final = register_measure_step(
    MeasureStep(
        name="aurora_pipeline_cost",
        measurement_type=GradeMeasurement,
        measure=measure_checkout,
        failed_measurement=INCOMPLETE_MEASUREMENT,  # an unusable grader output: a broken pipeline
    ),
)
