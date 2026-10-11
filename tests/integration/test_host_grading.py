"""A run played in one directory grades in another: a stack run plays in its container and grades on the host.

The main-task score is left out of the comparison: the company suite occasionally runs red on the same
checkout (#70), while the side task and the monitors' verdicts over the sealed log are deterministic.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from loc_arena import cli
from loc_arena.config import load_run_config
from loc_arena.harness import build_recorded_run_events, grade_run, locate_run, run_episode

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
RUN_SPECIFIC = {"run_name", "wall_clock_seconds", "main_task"}


def _comparable_scores(bundle: Path) -> dict[str, object]:
    scores = json.loads((bundle / "scores.json").read_text())
    return {key: value for key, value in scores.items() if key not in RUN_SPECIFIC}


@pytest.fixture(scope="module")
def in_process_scores(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    return _comparable_scores(
        run_episode(CONFIG, mode="attack", out_root=tmp_path_factory.mktemp("in-process"), seed=7),
    )


def _play_alone(out: Path, *options: str, mode: str = "attack") -> Path:
    """Play the deterministic run in ``mode`` with ``options``, as ``run --play-only`` does; its directory."""
    arguments = ["--mode", mode, "--play-only", *options, "--instance", "a1b2c3", "--out", str(out)]
    cli.main(["run", "--run", "aurora-efficiency.deterministic", *arguments])
    return locate_run(CONFIG, mode, "a1b2c3", out)


def test_a_run_played_alone_and_moved_grades_like_an_in_process_run(
    tmp_path: Path,
    in_process_scores: dict[str, object],
) -> None:
    played = _play_alone(tmp_path / "played")
    moved = tmp_path / "host" / played.name
    shutil.copytree(played, moved)
    shutil.rmtree(played)
    build_recorded_run_events(CONFIG, moved)

    bundle = grade_run(CONFIG, moved, mode="attack", seed=7)

    assert _comparable_scores(bundle) == in_process_scores


def test_the_first_play_of_an_attack_run_one_phase_at_a_time_leaves_the_honest_twin_unplayed(
    tmp_path: Path,
) -> None:
    played = _play_alone(tmp_path, "--next-phase")

    assert ((played / "episode").is_dir(), (played / "honest_cal").exists()) == (True, False)


def test_an_attack_run_played_one_phase_at_a_time_grades_like_an_in_process_run(
    tmp_path: Path,
    in_process_scores: dict[str, object],
) -> None:
    _play_alone(tmp_path, "--next-phase")
    played = _play_alone(tmp_path, "--next-phase")
    build_recorded_run_events(CONFIG, played)

    bundle = grade_run(CONFIG, played, mode="attack", seed=7)

    assert _comparable_scores(bundle) == in_process_scores


@pytest.mark.parametrize(("mode", "phases"), [("attack", 2), ("honest", 1)])
def test_a_run_that_played_every_phase_refuses_to_play_one_again_and_keeps_what_it_recorded(
    tmp_path: Path,
    mode: str,
    phases: int,
) -> None:
    played = [_play_alone(tmp_path, "--next-phase", mode=mode) for _ in range(phases)][-1]
    recorded = (played / "played.json").read_bytes()

    with pytest.raises(ValueError, match="nothing left to play"):
        _play_alone(tmp_path, "--next-phase", mode=mode)

    assert (played / "played.json").read_bytes() == recorded
