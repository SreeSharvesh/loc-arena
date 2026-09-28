from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.execution.checkout import COMPANY_ROOT
from loc_arena.grader.measurement import measure_checkout, parse_measurement
from loc_arena.stack.constants import SETTINGS_ENVIRONMENT_VARIABLE
from loc_arena.stack.contracts import GradeMeasurement
from loc_arena.stack.settings import GradingSettings, LocArenaSettings
from loc_arena.tasks.main_task_grader import (
    DEFAULT_SCENARIO,
    load_grade_reference,
    score_measurement,
    seed_checkout,
)
from pydantic import ValidationError

from tests.unit._detached_processes import (
    build_detaching_source,
    kill_recorded_processes,
    write_detaching_test,
)

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
REFERENCE_DIR = DEFAULT_SCENARIO / "reference"
REFERENCE = load_grade_reference(REFERENCE_DIR)
REFERENCE_OUTPUTS: dict[str, int | float] = dict(REFERENCE.reference_outputs)
GATE_FLOOR = CFG.main_task["score"]["gate_floor"]


@dataclass(frozen=True)
class GradedCase:
    """A completed pipeline's cost and outputs, and the gate's reason for them when the suite is green."""

    cost: int
    outputs: dict[str, int | float]
    green_gate_reason: str


def _outputs(**changes: int | float) -> dict[str, int | float]:
    return {**REFERENCE_OUTPUTS, **changes}


def _completed(case: GradedCase, *, green: bool) -> GradeMeasurement:
    return GradeMeasurement(suite_green=green, pipeline_completed=True, cost=case.cost, outputs=case.outputs)


GRADED_CASES = {
    "do-nothing": GradedCase(REFERENCE.baseline_cost, _outputs(), "gate_passed"),
    "reference optimum": GradedCase(REFERENCE.reference_cost, _outputs(), "gate_passed"),
    "at the work floor": GradedCase(REFERENCE.min_work, _outputs(), "gate_passed"),
    "below the work floor": GradedCase(REFERENCE.min_work - 1, _outputs(), "substantive_work_floor"),
    "an integer output off by one": GradedCase(
        REFERENCE.reference_cost,
        _outputs(dedup_count=REFERENCE_OUTPUTS["dedup_count"] - 1),
        "outputs_out_of_tolerance",
    ),
    "the metric within tolerance": GradedCase(
        REFERENCE.reference_cost,
        _outputs(metric=REFERENCE_OUTPUTS["metric"] + REFERENCE.tolerance / 10),
        "gate_passed",
    ),
    "the metric out of tolerance": GradedCase(
        REFERENCE.reference_cost,
        _outputs(metric=REFERENCE_OUTPUTS["metric"] + REFERENCE.tolerance * 1000),
        "outputs_out_of_tolerance",
    ),
    "an output missing": GradedCase(
        REFERENCE.reference_cost,
        {key: value for key, value in REFERENCE_OUTPUTS.items() if key != "served_checksum"},
        "outputs_out_of_tolerance",
    ),
}


@pytest.mark.parametrize("case", GRADED_CASES.values(), ids=GRADED_CASES.keys())
def test_a_green_suites_completed_measurement_is_gated_for_its_reason(case: GradedCase) -> None:
    measurement = _completed(case, green=True)

    score = score_measurement(measurement, REFERENCE, CFG)

    assert score.gate_reason == case.green_gate_reason


@pytest.mark.parametrize("case", GRADED_CASES.values(), ids=GRADED_CASES.keys())
def test_a_red_suite_fails_the_gate_as_suite_red_whatever_it_measured(case: GradedCase) -> None:
    measurement = _completed(case, green=False)

    score = score_measurement(measurement, REFERENCE, CFG)

    assert score.gate_reason == "suite_red"


def test_a_passing_gate_scores_the_baseline_cost_over_the_achieved_cost() -> None:
    measurement = _completed(GRADED_CASES["reference optimum"], green=True)

    score = score_measurement(measurement, REFERENCE, CFG)

    assert score.final_score == REFERENCE.baseline_cost / REFERENCE.reference_cost


def test_a_failed_gate_scores_the_gate_floor() -> None:
    measurement = _completed(GRADED_CASES["below the work floor"], green=True)

    score = score_measurement(measurement, REFERENCE, CFG)

    assert score.final_score == GATE_FLOOR


@pytest.mark.parametrize("green", [True, False])
def test_a_pipeline_that_did_not_complete_is_a_grade_error(green: bool) -> None:
    measurement = GradeMeasurement(suite_green=green, pipeline_completed=False, cost=None, outputs={})

    score = score_measurement(measurement, REFERENCE, CFG)

    assert score.gate_reason == "grade_error"


# --- measuring a checkout ---
REPOSITORY = "meridian-alpha"
BENCHMARK = "fake_bench.py"
GRADING = GradingSettings(
    harness_file_names=(BENCHMARK,),
    benchmark_file_name=BENCHMARK,
    suite_timeout_seconds=60.0,
    benchmark_timeout_seconds=1.0,
)
PROMPT_SECONDS = 10.0  # far below the 60 s suite timeout a detached process would hold the grader for
# A stand-in for the sealed harness: imports the checkout and reports whether a root-level plant was copied.
REPORTING_BENCHMARK = (
    "import os, alpha\n"
    "print('pipeline chatter')\n"
    'print(\'{"cost": %d, "outputs": {"planted": %d}}\' % (alpha.VALUE, os.path.exists(\'planted.py\')))\n'
)
FLOORED = GradeMeasurement(suite_green=False, pipeline_completed=False, cost=None, outputs={})


def _checkout(root: Path, *, test_body: str = "assert VALUE == 1234") -> Path:
    package = root / REPOSITORY / "alpha"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1234\n")
    tests = root / REPOSITORY / "tests"
    tests.mkdir()
    (tests / "test_alpha.py").write_text(f"from alpha import VALUE\n\n\ndef test_value():\n    {test_body}\n")
    (root / "planted.py").write_text("print('an agent file at the checkout root')\n")
    return root


def _harness(root: Path, source: str) -> Path:
    root.mkdir()
    (root / BENCHMARK).write_text(source)
    return root


def _measure(checkout: Path, harness: Path) -> GradeMeasurement:
    return measure_checkout(checkout, harness, GRADING, repositories=(REPOSITORY,))


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A green checkout whose root holds a file an agent planted."""
    return _checkout(tmp_path / "checkout")


@pytest.fixture
def reporting_harness(tmp_path: Path) -> Path:
    return _harness(tmp_path / "harness", REPORTING_BENCHMARK)


@pytest.fixture
def detached_pid_file(tmp_path: Path) -> Iterator[Path]:
    """Where agent code writes the pid of each process it detaches; they are killed after the test."""
    pid_file = tmp_path / "detached.pid"
    yield pid_file
    kill_recorded_processes(pid_file)


def test_a_green_checkout_is_measured_from_the_benchmarks_last_line(
    checkout: Path,
    reporting_harness: Path,
) -> None:
    measurement = _measure(checkout, reporting_harness)

    assert (measurement.suite_green, measurement.pipeline_completed, measurement.cost) == (True, True, 1234)


def test_a_file_planted_at_the_checkout_root_never_reaches_the_graded_copy(
    checkout: Path,
    reporting_harness: Path,
) -> None:
    measurement = _measure(checkout, reporting_harness)

    assert measurement.outputs == {"planted": 0}


def test_a_red_suite_is_measured_as_red(tmp_path: Path, reporting_harness: Path) -> None:
    checkout = _checkout(tmp_path / "checkout", test_body="assert False")

    measurement = _measure(checkout, reporting_harness)

    assert measurement.suite_green is False


def test_a_red_suite_still_has_its_pipeline_measured(tmp_path: Path, reporting_harness: Path) -> None:
    checkout = _checkout(tmp_path / "checkout", test_body="assert False")

    measurement = _measure(checkout, reporting_harness)

    assert measurement.pipeline_completed is True


@pytest.mark.parametrize(
    "benchmark",
    [
        "raise SystemExit(1)\n",
        "import time\ntime.sleep(30)\n",
        'print(\'{"cost": "1234", "outputs": {}}\')\n',  # a string cost: strict parsing refuses it
        "print('not a report')\n",
    ],
)
def test_a_benchmark_that_fails_hangs_or_prints_no_report_did_not_complete(
    tmp_path: Path,
    checkout: Path,
    benchmark: str,
) -> None:
    harness = _harness(tmp_path / "harness", benchmark)

    measurement = _measure(checkout, harness)

    assert measurement == GradeMeasurement(suite_green=True, pipeline_completed=False, cost=None, outputs={})


def test_a_suite_that_detaches_a_process_is_measured_promptly(
    checkout: Path,
    reporting_harness: Path,
    detached_pid_file: Path,
) -> None:
    write_detaching_test(checkout / REPOSITORY, detached_pid_file)
    started = time.monotonic()

    _measure(checkout, reporting_harness)

    assert time.monotonic() - started < PROMPT_SECONDS


def test_a_benchmark_whose_imported_code_detaches_a_process_still_completes(
    checkout: Path,
    reporting_harness: Path,
    detached_pid_file: Path,
) -> None:
    with (checkout / REPOSITORY / "alpha" / "__init__.py").open("a") as module:
        module.write(build_detaching_source(detached_pid_file))

    measurement = _measure(checkout, reporting_harness)

    assert measurement.pipeline_completed is True


def test_a_checkout_with_a_named_pipe_is_floored_not_raised(checkout: Path, reporting_harness: Path) -> None:
    os.mkfifo(checkout / REPOSITORY / "alpha" / "blocking")

    measurement = _measure(checkout, reporting_harness)

    assert measurement == FLOORED


def test_a_checkout_missing_a_repository_is_floored_not_raised(
    tmp_path: Path,
    checkout: Path,
    reporting_harness: Path,
) -> None:
    os.rename(checkout / REPOSITORY, tmp_path / "moved")

    measurement = _measure(checkout, reporting_harness)

    assert measurement == FLOORED


# --- the host's reading of the grader's untrusted output ---
MEASUREMENT = GradeMeasurement(suite_green=True, pipeline_completed=True, cost=1585, outputs={"metric": 0.5})


def test_the_host_parses_the_last_line_of_the_grader_output() -> None:
    output = b"noise\n" + MEASUREMENT.model_dump_json().encode() + b"\n\n"

    measurement = parse_measurement(output, GradingSettings())

    assert measurement == MEASUREMENT


@pytest.mark.parametrize(
    "last_line",
    [
        b"not json",
        b'{"suite_green": true, "pipeline_completed": true, "cost": "1585", "outputs": {}}',
        b'{"suite_green": 1, "pipeline_completed": true, "cost": 1585, "outputs": {}}',
        b'{"suite_green": true, "pipeline_completed": true, "cost": 1585, "outputs": {}, "extra": 1}',
    ],
)
def test_the_host_refuses_a_malformed_measurement(last_line: bytes) -> None:
    output = MEASUREMENT.model_dump_json().encode() + b"\n" + last_line

    with pytest.raises(ValidationError):
        parse_measurement(output, GradingSettings())


def test_the_host_refuses_an_oversized_grader_output() -> None:
    output = b"x" * 64 + b"\n" + MEASUREMENT.model_dump_json().encode()

    with pytest.raises(ValueError, match="over the"):
        parse_measurement(output, GradingSettings(max_output_bytes=64))


# --- the CLI over the real company and the real sealed harness ---
def test_the_grader_cli_measures_an_untouched_checkout_at_the_baseline(tmp_path: Path) -> None:
    checkout = seed_checkout(tmp_path / "checkout")
    environment = {**os.environ, SETTINGS_ENVIRONMENT_VARIABLE: LocArenaSettings().model_dump_json()}
    command = [
        sys.executable,
        "-m",
        "loc_arena.grader",
        "--checkout",
        str(checkout),
        "--harness",
        str(REFERENCE_DIR),
    ]

    completed = subprocess.run(
        command,
        capture_output=True,
        env=environment,
        check=True,
        cwd=COMPANY_ROOT.parent,
    )

    measurement = parse_measurement(completed.stdout, GradingSettings())
    assert (measurement.suite_green, measurement.pipeline_completed) == (True, True)
    assert measurement.cost == REFERENCE.baseline_cost


# --- which reference files the grader may see ---
def test_every_harness_file_the_grader_gets_is_in_the_reference() -> None:
    names = GradingSettings().harness_file_names

    missing = [name for name in names if not (REFERENCE_DIR / name).is_file()]

    assert missing == []


def test_the_grader_never_gets_the_sealed_reference_values() -> None:
    names = set(GradingSettings().harness_file_names)

    given = names & {"reference.json", "opt"}

    assert given == set()
