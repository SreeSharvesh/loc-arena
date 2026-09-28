"""The grader CLI: measure one checkout and print its ``GradeMeasurement`` JSON as the last stdout line.

``python -m loc_arena.grader [--checkout PATH] [--harness PATH]``. In the stack it runs in the grader
container with no network, the checkout and the sealed harness files mounted read-only at the defaults,
and the settings in ``LOC_ARENA_SETTINGS``. The host reads the line as untrusted (``parse_measurement``).
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from loc_arena.execution.checkout import COMPANY_ROOT, list_repositories
from loc_arena.grader.measurement import measure_checkout
from loc_arena.stack.constants import GRADER_HARNESS_MOUNT_PATH, WORKSPACE_MOUNT_PATH
from loc_arena.stack.settings import load_settings_from_environment


def main(arguments: Sequence[str] | None = None) -> None:
    """Parse ``arguments`` (the command line by default), measure the checkout, print the measurement."""
    parser = argparse.ArgumentParser(prog="python -m loc_arena.grader", description=__doc__)
    parser.add_argument("--checkout", type=Path, default=WORKSPACE_MOUNT_PATH, help="the checkout to measure")
    parser.add_argument(
        "--harness",
        type=Path,
        default=GRADER_HARNESS_MOUNT_PATH,
        help="the directory holding the sealed harness files",
    )
    parsed = parser.parse_args(arguments)
    settings = load_settings_from_environment()
    measurement = measure_checkout(
        parsed.checkout,
        parsed.harness,
        settings.grading,
        repositories=list_repositories(COMPANY_ROOT),  # the image's pristine list, not the checkout's
    )
    print(measurement.model_dump_json())


if __name__ == "__main__":
    main()
