"""A played episode is graded again from its directory alone, through ``snapshot.json``.

The main-task score is left out of the comparison with the run's stored scores: the company suite
occasionally runs red on the same checkout (#70), while the side-task verdict over the sealed log is
deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from loc_arena import cli
from loc_arena.task import SNAPSHOT_FILE, read_snapshot

from tests._stored_run import unpack_stored_run
from tests.integration._records_support import LIVE, play


def test_the_grade_command_writes_the_side_task_verdict_the_run_was_graded_with(tmp_path: Path) -> None:
    run_directory = unpack_stored_run(tmp_path)
    stored = json.loads((run_directory / "scores.json").read_text())["side_task"]

    exit_code = cli.main(["grade", "--run", "aurora-efficiency.fast", str(run_directory / "episode")])

    graded = json.loads((run_directory / "episode" / "grades.json").read_text())["side_task"]
    evidence = {name: milestone["evidence"] for name, milestone in graded["milestones"].items()}
    assert (exit_code, graded["covert_depth"], graded["landing"]["minimal"], evidence) == (
        0,
        stored["covert_depth"],
        stored["landing_minimal"],
        {name: milestone["evidence"] for name, milestone in stored["milestones"].items()},
    )


def test_a_snapshot_pointing_outside_its_directory_is_refused(tmp_path: Path) -> None:
    episode = play(tmp_path / "run") / "episode"
    stored = json.loads((episode / SNAPSHOT_FILE).read_text())
    (episode / SNAPSHOT_FILE).write_text(json.dumps({**stored, "checkout": "../../elsewhere"}))

    with pytest.raises(ValueError, match="elsewhere is outside"):
        read_snapshot(episode, LIVE)
