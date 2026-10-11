"""A played episode can be graded again from its directory alone, through snapshot.json.

The main-task score is left out of comparisons between two gradings: the company suite occasionally runs red
on the same checkout (#70), while the side-task verdict over the sealed log is deterministic.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path

import pytest
from loc_arena import cli
from loc_arena.config import load_run_config
from loc_arena.harness import apply_mode
from loc_arena.live import play_model_episode
from loc_arena.task import (
    grade_snapshot,
    read_snapshot,
    read_snapshot_file,
    record_snapshot,
)

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

    assert regraded.verdict.landing.robust
    assert regraded.sealed_path.is_relative_to(tmp_path / "moved")


@ATTACK_MOVES_PENDING
def test_the_grade_command_writes_the_verdict_of_the_stored_mode(tmp_path: Path) -> None:
    played = play_model_episode(CONFIG, tmp_path / "episode")
    graded = grade_snapshot(played, CONFIG)

    exit_code = cli.main(["grade", "--run", "aurora-efficiency.deterministic", str(tmp_path / "episode")])

    grades = json.loads((tmp_path / "episode" / "grades.json").read_text())
    assert (exit_code, grades["side_task"]["covert_depth"]) == (0, graded.verdict.covert_depth)
    assert grades["side_task"]["landing"]["robust"] is True


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


def test_a_checkout_played_in_the_sandboxs_volume_is_moved_into_its_episode_directory_links_kept(
    tmp_path: Path,
) -> None:
    played = play_model_episode(CONFIG, tmp_path / "episode")
    in_volume = tmp_path / "checkouts" / "checkout"
    shutil.move(played.checkout, in_volume)
    (in_volume / "a-link").symlink_to("meridian-common")
    played_there = dataclasses.replace(played, checkout=in_volume)

    kept = record_snapshot(played_there, tmp_path / "episode", CONFIG).checkout

    assert (kept, (kept / "a-link").readlink(), in_volume.exists()) == (
        tmp_path / "episode" / "checkout",
        Path("meridian-common"),
        False,
    )


def test_a_checkout_whose_path_became_a_link_is_refused(tmp_path: Path) -> None:
    played = play_model_episode(CONFIG, tmp_path / "episode")
    shutil.move(played.checkout, tmp_path / "elsewhere")
    (tmp_path / "checkouts").symlink_to(tmp_path / "elsewhere")
    played_through_a_link = dataclasses.replace(played, checkout=tmp_path / "checkouts")

    with pytest.raises(ValueError, match="no longer resolves to itself"):
        record_snapshot(played_through_a_link, tmp_path / "episode", CONFIG)


def test_an_absolute_link_into_the_moved_checkout_points_into_it_relatively_and_others_stay(
    tmp_path: Path,
) -> None:
    played = play_model_episode(CONFIG, tmp_path / "episode")
    in_volume = tmp_path / "checkouts" / "checkout"
    shutil.move(played.checkout, in_volume)
    (in_volume / "meridian-common" / "to-serving").symlink_to(in_volume / "meridian-serving")
    (in_volume / "to-elsewhere").symlink_to(tmp_path / "elsewhere")
    played_there = dataclasses.replace(played, checkout=in_volume)

    kept = record_snapshot(played_there, tmp_path / "episode", CONFIG).checkout

    targets = ((kept / "meridian-common" / "to-serving").readlink(), (kept / "to-elsewhere").readlink())
    assert targets == (Path("../meridian-serving"), tmp_path / "elsewhere")
