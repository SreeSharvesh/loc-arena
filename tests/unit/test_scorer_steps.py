"""A main-task scorer: a measure step and a score step under one name, graded through one parse."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from loc_arena.config import RunConfig, load_run_config
from loc_arena.grader.measure_steps import (
    MEASURE_STEPS,
    MeasurementRequest,
    MeasureStep,
    UnknownMeasureStepError,
    get_measure_step,
    register_measure_step,
)
from loc_arena.registry import SCORER_REGISTRY, ScorerSteps, register_scorer
from loc_arena.stack.contracts import ContractModel
from loc_arena.tasks.main_task_grader import MainTaskScore

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
NAME = "unit_line_count"
FAILED_LINES = -1


class LineCount(ContractModel):
    """A measurement shaped unlike Aurora's: the lines of one file."""

    lines: int


def _count_lines(request: MeasurementRequest) -> LineCount:
    return LineCount(lines=len((request.checkout / "notes.txt").read_text().splitlines()))


def _score(measurement: LineCount, config: RunConfig, reference_directory: Path) -> MainTaskScore:
    return MainTaskScore(
        final_score=float(measurement.lines),
        milestones_reached=[],
        frac_human=0.0,
        achieved_cost=0,
        baseline_cost=0,
        gate_passed=measurement.lines >= 0,
        gate_reason="unit",
    )


def _step() -> MeasureStep[LineCount]:
    return MeasureStep(
        name=NAME,
        measurement_type=LineCount,
        measure=_count_lines,
        failed_measurement=LineCount(lines=FAILED_LINES),
    )


@pytest.fixture(autouse=True)
def restored_registries() -> Iterator[None]:
    """Each test registers into the process-wide registries; put them back as they were afterwards."""
    measure_steps, scorers = dict(MEASURE_STEPS), dict(SCORER_REGISTRY)
    yield
    MEASURE_STEPS.clear()
    MEASURE_STEPS.update(measure_steps)
    SCORER_REGISTRY.clear()
    SCORER_REGISTRY.update(scorers)


@pytest.fixture
def scorer() -> ScorerSteps[LineCount]:
    steps = ScorerSteps(measure_step=register_measure_step(_step()), score=_score)
    register_scorer(steps)
    return steps


def test_a_second_measure_step_under_a_taken_name_is_refused() -> None:
    register_measure_step(_step())

    with pytest.raises(ValueError, match="already registered"):
        register_measure_step(_step())


def test_an_unknown_measure_step_is_refused_listing_the_registered_ones() -> None:
    register_measure_step(_step())

    with pytest.raises(UnknownMeasureStepError, match=NAME):
        get_measure_step("no_such_scorer")


def test_a_scorer_whose_measure_step_the_grader_cannot_find_is_refused() -> None:
    register_measure_step(_step())
    unregistered_step = _step()

    with pytest.raises(ValueError, match="other than the one registered"):
        register_scorer(ScorerSteps(measure_step=unregistered_step, score=_score))


def test_a_second_scorer_under_a_taken_name_is_refused(scorer: ScorerSteps[LineCount]) -> None:
    rival = ScorerSteps(measure_step=scorer.measure_step, score=_score)

    with pytest.raises(ValueError, match="already registered"):
        register_scorer(rival)


def test_an_in_process_grade_scores_what_the_measure_step_measured(
    scorer: ScorerSteps[LineCount],
    tmp_path: Path,
) -> None:
    (tmp_path / "notes.txt").write_text("one\ntwo\nthree\n")

    score = scorer.grade_checkout(tmp_path, (), CONFIG, tmp_path)

    assert score.final_score == 3.0


def test_the_last_line_of_a_grader_output_is_scored(scorer: ScorerSteps[LineCount], tmp_path: Path) -> None:
    output = b"a suite printed this\n" + LineCount(lines=7).model_dump_json().encode() + b"\n"

    score = scorer.grade_output(output, CONFIG, tmp_path)

    assert score.final_score == 7.0


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"not json",
        b'{"lines": "7"}',  # a string count: strict parsing refuses it
        b'{"lines": 7, "extra": 1}',
        b'{"suite_green": true, "pipeline_completed": true, "cost": 1, "outputs": {}}',  # another scorer's
    ],
)
def test_an_output_that_is_no_measurement_of_this_scorer_scores_its_failed_measurement(
    scorer: ScorerSteps[LineCount],
    tmp_path: Path,
    output: bytes,
) -> None:
    score = scorer.grade_output(output, CONFIG, tmp_path)

    assert score.final_score == FAILED_LINES


def test_an_output_over_the_cap_scores_the_failed_measurement(
    scorer: ScorerSteps[LineCount],
    tmp_path: Path,
) -> None:
    cap = CONFIG.settings.grading.max_output_bytes
    output = b"x" * cap + b"\n" + LineCount(lines=7).model_dump_json().encode()

    score = scorer.grade_output(output, CONFIG, tmp_path)

    assert score.final_score == FAILED_LINES
