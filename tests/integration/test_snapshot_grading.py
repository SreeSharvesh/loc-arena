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
from loc_arena.live import play_model_episode
from loc_arena.task import grade_snapshot, read_snapshot, read_snapshot_file

from tests.integration._live_support import ATTACK_MOVES_PENDING

CONFIG = apply_mode(load_run_config("configs/aurora-efficiency.deterministic.yaml"), "attack")


def test_a_snapshot_read_back_from_its_directory_grades_like_the_one_played(tmp_path: Path) -> None:
    played = play_model_episode(CONFIG, tmp_path / "episode")
    graded = grade_snapshot(played, CONFIG)

    regraded = grade_snapshot(read_snapshot(tmp_path / "episode", CONFIG), CONFIG)

    assert (regraded.checkout, regraded.verdict) == (played.checkout, graded.verdict)


@ATTACK_MOVES_PENDING
def test_a_snapshot_directory_still_grades_after_it_is_moved(tmp_path: Path) -> None:
    play_model_episode(CONFIG, tmp_path / "episode")
    shutil.copytree(tmp_path / "episode", tmp_path / "moved")
    shutil.rmtree(tmp_path / "episode")

    regraded = grade_snapshot(read_snapshot(tmp_path / "moved", CONFIG), CONFIG)

    assert regraded.verdict.landing.minimal
    assert regraded.sealed_path.is_relative_to(tmp_path / "moved")


@ATTACK_MOVES_PENDING
def test_the_grade_command_writes_the_verdict_of_the_stored_mode(tmp_path: Path) -> None:
    played = play_model_episode(CONFIG, tmp_path / "episode")
    graded = grade_snapshot(played, CONFIG)

    exit_code = cli.main(["grade", "--run", "aurora-efficiency.deterministic", str(tmp_path / "episode")])

    grades = json.loads((tmp_path / "episode" / "grades.json").read_text())
    assert (exit_code, grades["side_task"]["covert_depth"]) == (0, graded.verdict.covert_depth)
    assert grades["side_task"]["landing"]["minimal"] is True


@pytest.mark.parametrize("mode", ["attack", "honest"])
def test_a_snapshot_records_the_mode_the_episode_was_played_in(tmp_path: Path, mode: str) -> None:
    config = apply_mode(CONFIG, mode)
    play_model_episode(config, tmp_path / "episode")

    snapshot_file = read_snapshot_file(tmp_path / "episode")

    assert snapshot_file.mode == mode


def test_a_snapshot_pointing_outside_its_directory_is_refused(tmp_path: Path) -> None:
    play_model_episode(CONFIG, tmp_path / "episode")
    snapshot_path = tmp_path / "episode" / "snapshot.json"
    stored = json.loads(snapshot_path.read_text())
    snapshot_path.write_text(json.dumps({**stored, "checkout": "../../elsewhere"}))

    with pytest.raises(ValueError, match="elsewhere is outside"):
        read_snapshot(tmp_path / "episode", CONFIG)
