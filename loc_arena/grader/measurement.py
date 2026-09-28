"""What the grader measures on one checkout: its suites, and the sealed benchmark's cost and outputs.

This half of the main-task grade needs no network and no reference: it runs in the grader container
(``sandbox`` image, ``network_mode: none``) or, for STACK=0, on the host. The host scores the measurement
against the sealed ``reference.json`` (``tasks.main_task_grader.score_measurement``), and reads the
grader's output as untrusted (``parse_measurement``): agent code runs in the grader.

Only the trusted list of repositories is copied, into an empty grading root, so nothing an agent left at
the checkout's root (a shadowing module, a symlink named like a harness file) reaches the graded copy.
The sealed harness files are then placed at that root, never inside a repository. Every suite and the
benchmark run through :func:`~loc_arena.execution.command.run_command`, so a process agent code detaches
never holds the grader past the run that started it. An agent can still influence the measured cost from
code the pipeline imports, or from a process a suite detached that is still running during the benchmark;
that residual risk is accepted, bounded by the reference's ``min_work`` floor.
"""

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


class BenchmarkReport(ContractModel):
    """The sealed benchmark's last stdout line: the metered cost and the pipeline's outputs."""

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
    """Measure ``checkout``: every repository's suite, then the sealed benchmark over the pipeline.

    A checkout that cannot be copied (a repository missing, a named pipe) measures as red and incomplete;
    so does a benchmark that hangs, fails or prints no valid report. Nothing an agent does raises.
    """
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
    """Whether every repository's suite passes; stops at the first red one (a hang counts as red)."""
    for repository in checkout.repositories:
        completed = checkout.run_suite(
            repository,
            timeout_seconds=settings.suite_timeout_seconds,
            max_output_characters=settings.max_captured_output_characters,
        )
        if completed.exit_code != 0:  # red, or None: killed at its timeout
            return False
    return True


def _run_benchmark(checkout: Checkout, settings: GradingSettings) -> BenchmarkReport | None:
    """The sealed benchmark's report; ``None`` when the pipeline hung, failed or printed no valid report."""
    completed = checkout.run_python(
        (settings.benchmark_file_name,),
        cwd=checkout.root,
        timeout_seconds=settings.benchmark_timeout_seconds,
        max_output_characters=settings.max_captured_output_characters,
    )
    if completed.exit_code != 0:  # failed, or None: killed at its timeout
        return None
    try:
        return BenchmarkReport.model_validate_json(extract_last_line(completed.stdout.text), strict=True)
    except ValidationError:
        return None
