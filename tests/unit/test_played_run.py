"""``played.json`` carries what play recorded to grading as JSON: the traces and the phases played."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest
from loc_arena import cli, live
from loc_arena.config import load_run_config
from loc_arena.harness import PLAYED_FILE, FailedModelCalls, PlayedRun, WallClockCeiling, locate_run, play_run
from loc_arena.logging_.agent_trace import EpisodeTrace, ModelCall, TurnRecord, TurnRef
from loc_arena.task import Snapshot

TURN = TurnRef("agent-main", 0)
TRACE = EpisodeTrace(
    turns=(TurnRecord(TURN, wall_start=1.25, wall_end=2.5),),
    sealed_lane=MappingProxyType({0: TURN, 1: None}),
    mirror_lane=MappingProxyType({0: TURN}),
    mirror_to_sealed=MappingProxyType({0: 0}),
    model_calls=(ModelCall("executing", "agent-main", "untrusted_agent", "prompt", "reply", 1, 2.0),),
    last_sealed_seq=1,
)


def test_a_played_run_written_as_json_reads_back_with_the_traces_it_was_given() -> None:
    played = PlayedRun(play_seconds=3.5, episode_trace=TRACE, calibration_trace=TRACE)

    read_back = PlayedRun.model_validate_json(played.model_dump_json())

    assert (read_back.play_seconds, read_back.episode_trace, read_back.calibration_trace) == (
        3.5,
        TRACE,
        TRACE,
    )


@pytest.mark.parametrize("instance_id", ["../../outside", "a/b", "a\\b", ""])
def test_a_run_instance_that_could_leave_the_output_directory_is_refused(
    tmp_path: Path,
    instance_id: str,
) -> None:
    config = load_run_config("configs/aurora-efficiency.yaml")

    with pytest.raises(ValueError, match="letters and digits"):
        locate_run(config, "attack", instance_id, tmp_path)


def test_a_run_directory_is_absolute_so_commands_run_elsewhere_find_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_run_config("configs/aurora-efficiency.yaml")
    monkeypatch.chdir(tmp_path)

    run_directory = locate_run(config, "attack", "a1b2c3", Path("logs"))

    assert run_directory.parent.parent == tmp_path.resolve() / "logs"


@pytest.mark.parametrize(("mode", "twin_failed_calls"), [("attack", 0), ("honest", None)])
def test_a_run_that_played_every_phase_refuses_to_play_one_again_and_keeps_what_it_recorded(
    tmp_path: Path,
    mode: str,
    twin_failed_calls: int | None,
) -> None:
    run_directory = locate_run(load_run_config("configs/aurora-efficiency.yaml"), mode, "a1b2c3", tmp_path)
    run_directory.mkdir(parents=True)
    played = PlayedRun(
        play_seconds=3.5,
        episode_trace=None,
        calibration_trace=None,
        failed_model_calls=FailedModelCalls(episode=0, calibration=twin_failed_calls),
    )
    (run_directory / PLAYED_FILE).write_text(played.model_dump_json())
    play_next = ["run", "--mode", mode, "--play-only", "--next-phase", "--instance", "a1b2c3"]

    with pytest.raises(ValueError, match="nothing left to play"):
        cli.main([*play_next, "--out", str(tmp_path)])

    assert PlayedRun.model_validate_json((run_directory / PLAYED_FILE).read_text()) == played


CONFIG = load_run_config("configs/aurora-efficiency.yaml")


@pytest.fixture
def plays(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    """Each play made, as (its directory, whether covert was on): a play needs a stack, so one stands for it.

    The stand-in's episode is stopped at its ceiling with 2 failed model calls, and its twin has 3.
    """
    made: list[tuple[str, bool]] = []

    def play(config: object, workdir: Path) -> Snapshot:
        made.append((workdir.name, config.covert.enabled))  # ty: ignore[unresolved-attribute]
        episode = workdir.name == "episode"
        return Snapshot(
            workdir / "sealed",
            workdir / "mirror",
            workdir / "checkout",
            0.0,
            stopped_at_wall_clock_ceiling=episode,
            failed_model_calls=2 if episode else 3,
        )

    monkeypatch.setattr(live, "play_model_episode", play)
    return made


def test_the_first_play_of_an_attack_run_one_phase_at_a_time_leaves_the_honest_twin_unplayed(
    tmp_path: Path,
    plays: list[tuple[str, bool]],
) -> None:
    play_run(CONFIG, tmp_path, mode="attack", next_phase=True)

    played = PlayedRun.model_validate_json((tmp_path / PLAYED_FILE).read_text())
    assert plays == [("episode", True)]
    assert played.failed_model_calls == FailedModelCalls(episode=2, calibration=None)


def test_the_second_play_is_the_honest_twin_and_keeps_what_the_first_recorded(
    tmp_path: Path,
    plays: list[tuple[str, bool]],
) -> None:
    play_run(CONFIG, tmp_path, mode="attack", next_phase=True)
    plays.clear()

    play_run(CONFIG, tmp_path, mode="attack", next_phase=True)

    played = PlayedRun.model_validate_json((tmp_path / PLAYED_FILE).read_text())
    seconds = CONFIG.episode.wall_clock_seconds
    assert plays == [("honest_cal", False)]
    assert (played.failed_model_calls, played.wall_clock_ceiling) == (
        FailedModelCalls(episode=2, calibration=3),
        WallClockCeiling(seconds=seconds, episode_stopped=True, calibration_stopped=False),
    )
