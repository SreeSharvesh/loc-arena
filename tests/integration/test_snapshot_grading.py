"""A played episode can be graded again from its directory alone, through snapshot.json.

The main-task score is left out of comparisons between two gradings: the company suite occasionally runs red
on the same checkout (#70), while the side-task verdict over the sealed log is deterministic.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from loc_arena import cli
from loc_arena.config import load_run_config
from loc_arena.harness import apply_mode
from loc_arena.task import grade_snapshot, play_scripted_episode, read_snapshot, read_snapshot_file

CONFIG = apply_mode(load_run_config("configs/aurora-efficiency.deterministic.yaml"), "attack")


def test_a_snapshot_read_back_from_its_directory_grades_like_the_one_played(tmp_path: Path) -> None:
    played = play_scripted_episode(CONFIG, tmp_path / "episode", robust=True)
    graded = grade_snapshot(played, CONFIG)

    regraded = grade_snapshot(read_snapshot(tmp_path / "episode", CONFIG), CONFIG)

    assert (regraded.checkout, regraded.verdict) == (played.checkout, graded.verdict)


def test_a_snapshot_directory_still_grades_after_it_is_moved(tmp_path: Path) -> None:
    play_scripted_episode(CONFIG, tmp_path / "episode", robust=True)
    shutil.copytree(tmp_path / "episode", tmp_path / "moved")
    shutil.rmtree(tmp_path / "episode")

    regraded = grade_snapshot(read_snapshot(tmp_path / "moved", CONFIG), CONFIG)

    assert regraded.verdict.landing.robust
    assert regraded.sealed_path.is_relative_to(tmp_path / "moved")


def test_the_grade_command_writes_the_verdict_of_the_stored_mode(tmp_path: Path) -> None:
    played = play_scripted_episode(CONFIG, tmp_path / "episode", robust=True)
    graded = grade_snapshot(played, CONFIG)

    exit_code = cli.main(["grade", "--run", "aurora-efficiency.deterministic", str(tmp_path / "episode")])

    grades = json.loads((tmp_path / "episode" / "grades.json").read_text())
    assert (exit_code, grades["side_task"]["covert_depth"]) == (0, graded.verdict.covert_depth)
    assert grades["side_task"]["landing"]["robust"] is True


@pytest.mark.parametrize("mode", ["attack", "honest"])
def test_a_snapshot_records_the_mode_the_episode_was_played_in(tmp_path: Path, mode: str) -> None:
    config = apply_mode(CONFIG, mode)
    play_scripted_episode(config, tmp_path / "episode")

    snapshot_file = read_snapshot_file(tmp_path / "episode")

    assert snapshot_file.mode == mode
