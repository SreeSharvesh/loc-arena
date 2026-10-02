"""Measures one checkout: every repository's suite, then the sealed benchmark's cost and outputs."""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from loc_arena.execution.checkout import Checkout, copy_repositories, extract_last_line
from loc_arena.stack.contracts import ContractModel, GradeMeasurement
from loc_arena.stack.settings import GradingSettings


class _BenchmarkReport(ContractModel):
    cost: int
    outputs: dict[str, int | float]


def measure_checkout(
    checkout: Path,
    harness_directory: Path,
    settings: GradingSettings,
    *,
    repositories: Sequence[str],
    python_executable: str = sys.executable,
) -> GradeMeasurement:
    """Measure a checkout; see docs/isolation/design.md#grader."""
    with tempfile.TemporaryDirectory(prefix="locarena-grade-", ignore_cleanup_errors=True) as grading_root:
        grading = Checkout(Path(grading_root), tuple(repositories), python_executable)
        try:
            copy_repositories(checkout, grading.root, repositories)
        except OSError:  # shutil.Error included
            return GradeMeasurement(suite_green=False, pipeline_completed=False, cost=None, outputs={})
        suite_green = _is_suite_green(grading, settings)
        for name in settings.harness_file_names:
            shutil.copy(harness_directory / name, grading.root / name)
        report = _run_benchmark(grading, settings)
    if report is None:
        return GradeMeasurement(suite_green=suite_green, pipeline_completed=False, cost=None, outputs={})
    return GradeMeasurement(
        suite_green=suite_green,
        pipeline_completed=True,
        cost=report.cost,
        outputs=report.outputs,
    )


def parse_measurement(output: bytes, settings: GradingSettings) -> GradeMeasurement:
    """The grader's measurement from its stdout, read as untrusted: size-capped, last line, strict types.

    Raises ``ValueError`` (pydantic's ``ValidationError`` included) when the output exceeds
    ``max_output_bytes`` or its last line is not exactly a ``GradeMeasurement``.
    """
    if len(output) > settings.max_output_bytes:
        raise ValueError(f"the grader printed {len(output)} bytes, over the {settings.max_output_bytes} cap")
    return GradeMeasurement.model_validate_json(
        extract_last_line(output.decode(errors="replace")),
        strict=True,
    )


def _is_suite_green(checkout: Checkout, settings: GradingSettings) -> bool:
    for repository in checkout.repositories:
        completed = checkout.run_suite(
            repository,
            timeout_seconds=settings.suite_timeout_seconds,
            max_output_characters=settings.max_captured_output_characters,
        )
        if completed.exit_code != 0:  # red, or None: killed at its timeout
            return False
    return True


def _run_benchmark(checkout: Checkout, settings: GradingSettings) -> _BenchmarkReport | None:
    completed = checkout.run_python(
        (settings.benchmark_file_name,),
        cwd=checkout.root,
        timeout_seconds=settings.benchmark_timeout_seconds,
        max_output_characters=settings.max_captured_output_characters,
    )
    if completed.exit_code != 0:  # failed, or None: killed at its timeout
        return None
    try:
        return _BenchmarkReport.model_validate_json(extract_last_line(completed.stdout.text), strict=True)
    except ValidationError:
        return None
