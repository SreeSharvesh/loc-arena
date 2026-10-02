"""The grader CLI: measure one checkout and print its measurement as the last stdout line."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from loc_arena.execution.checkout import COMPANY_ROOT, list_repositories
from loc_arena.grader.measure_steps import MeasurementRequest, get_measure_step, load_measure_module
from loc_arena.stack.constants import (
    GRADER_HARNESS_MOUNT_PATH,
    GRADER_MEASURE_MODULE_MOUNT_PATH,
    MAIN_TASK_SCORER_ENVIRONMENT_VARIABLE,
    WORKSPACE_MOUNT_PATH,
)
from loc_arena.stack.settings import load_settings_from_environment


def main() -> None:
    """Parse the command line, measure the checkout, print the measurement."""
    parser = argparse.ArgumentParser(prog="python -m loc_arena.grader", description=__doc__)
    parser.add_argument("--checkout", type=Path, default=WORKSPACE_MOUNT_PATH, help="the checkout to measure")
    parser.add_argument(
        "--harness",
        type=Path,
        default=GRADER_HARNESS_MOUNT_PATH,
        help="the directory holding the sealed harness files",
    )
    parser.add_argument(
        "--measure-module",
        type=Path,
        default=GRADER_MEASURE_MODULE_MOUNT_PATH,
        help="the scenario's measure.py, which registers its measure steps",
    )
    parser.add_argument(
        "--scorer",
        default=os.environ.get(MAIN_TASK_SCORER_ENVIRONMENT_VARIABLE),
        help=f"the scorer whose measure step runs (default: ${MAIN_TASK_SCORER_ENVIRONMENT_VARIABLE})",
    )
    parsed = parser.parse_args()
    if parsed.scorer is None:
        parser.error(f"name the scorer with --scorer or {MAIN_TASK_SCORER_ENVIRONMENT_VARIABLE}")
    settings = load_settings_from_environment()
    load_measure_module(parsed.measure_module)
    step = get_measure_step(parsed.scorer)
    request = MeasurementRequest(
        checkout=parsed.checkout,
        harness_directory=parsed.harness,
        repositories=list_repositories(COMPANY_ROOT),
        settings=settings.grading,
    )
    print(step.measure_as_json(request))


if __name__ == "__main__":
    main()
