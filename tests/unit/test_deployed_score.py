"""The main-task score of what a play deployed, beside the score of what it merged."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from loc_arena.config import RunConfig, load_run_config
from loc_arena.deployed_score import DeployedScore, grade_deployed
from loc_arena.merged_main import DEPLOYED_CHECKOUT, DEPLOYED_FILE, DeployedFile
from loc_arena.registry import SCORER_REGISTRY
from loc_arena.tasks.main_task_grader import MainTaskScore

MERGED = MainTaskScore(0.8, [], 0.5, 100, 125, gate_passed=True, gate_reason="")
GRADED_ANEW = dataclasses.replace(MERGED, final_score=0.25)


@pytest.fixture
def graded(monkeypatch: pytest.MonkeyPatch) -> tuple[RunConfig, list[Path]]:
    """A run config whose main-task scorer scores any checkout 0.25, and the checkouts it was given."""
    checkouts: list[Path] = []

    def score(checkout: Path, _config: RunConfig, _reference: Path) -> MainTaskScore:
        checkouts.append(checkout)
        return GRADED_ANEW

    monkeypatch.setitem(SCORER_REGISTRY, "a-fixed-score", score)
    config = load_run_config("configs/aurora-efficiency.yaml")
    return dataclasses.replace(config, main_task={**config.main_task, "scorer": "a-fixed-score"}), checkouts


def test_a_play_that_deployed_nothing_scores_nothing_for_what_it_deployed(
    graded: tuple[RunConfig, list[Path]],
    tmp_path: Path,
) -> None:
    config, _ = graded

    score = grade_deployed(tmp_path, config, MERGED)

    assert score == DeployedScore(0.0, None, None, [])


def test_a_deployment_behind_main_in_no_repo_scores_what_main_scored_and_is_not_graded_again(
    graded: tuple[RunConfig, list[Path]],
    tmp_path: Path,
) -> None:
    config, checkouts = graded
    said = DeployedFile(deployment=2, service="chat", behind_main=[])
    (tmp_path / DEPLOYED_FILE).write_text(said.model_dump_json())

    score = grade_deployed(tmp_path, config, MERGED)

    assert (score, checkouts) == (DeployedScore(0.8, 2, "chat", []), [])


def test_a_deployment_behind_main_is_graded_on_the_trees_it_released(
    graded: tuple[RunConfig, list[Path]],
    tmp_path: Path,
) -> None:
    config, checkouts = graded
    said = DeployedFile(deployment=1, service="notes", behind_main=["meridian-serving"])
    (tmp_path / DEPLOYED_FILE).write_text(said.model_dump_json())

    score = grade_deployed(tmp_path, config, MERGED)

    assert (score, checkouts) == (
        DeployedScore(0.25, 1, "notes", ["meridian-serving"]),
        [tmp_path / DEPLOYED_CHECKOUT],
    )
