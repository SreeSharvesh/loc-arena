"""A model-policy run ends: no turn starts past the wall-clock ceiling, and play survives a dead provider."""

from __future__ import annotations

import dataclasses
import itertools
import json
from pathlib import Path

import pytest
from loc_arena.config import RunConfig, load_run_config
from loc_arena.gateway.core import API_KEY_VARIABLE, GATEWAY_URL_VARIABLE
from loc_arena.harness import run_episode
from loc_arena.live import play_model_episode

from tests.integration._gateway_support import FailingProvider

LIVE = dataclasses.replace(load_run_config("configs/aurora-efficiency.deterministic.yaml"), policy="model")
AGENT_MODEL = LIVE.models["untrusted_agent"].model
# A clock that reads 0 when the episode starts and one second more at each later reading: with a ceiling of 3
# seconds, the turns checked at 1 and 2 seconds start, and the check at 3 stops the team.
CEILING_SECONDS = 3
TURNS_BEFORE_THE_CEILING = 2
PASSED_CEILING_SECONDS = 0  # the ceiling has passed before the first turn
# Under a dead provider each episode fails the coworker's batch call, then each of its 7 agents' calls until
# agent_loop.failed_turns_before_end (5) turns in a row have failed: 1 + 7 * 5.
FAILED_CALLS_PER_EPISODE = 36


def _with_ceiling(seconds: int) -> RunConfig:
    return dataclasses.replace(LIVE, episode=dataclasses.replace(LIVE.episode, wall_clock_seconds=seconds))


@pytest.fixture(autouse=True)
def no_live_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(API_KEY_VARIABLE, raising=False)
    monkeypatch.delenv(GATEWAY_URL_VARIABLE, raising=False)


def _run_under_a_dead_provider(config: RunConfig, out_root: Path) -> dict[str, object]:
    bundle = run_episode(
        config,
        mode="attack",
        out_root=out_root,
        provider=FailingProvider(),
        calibration_provider=FailingProvider(),
        write_report=False,
    )
    return json.loads((bundle / "scores.json").read_text())


def test_an_episode_starts_no_turn_once_the_clock_passes_its_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("loc_arena.live.time.monotonic", itertools.count().__next__)
    provider = FailingProvider()

    play_model_episode(_with_ceiling(CEILING_SECONDS), tmp_path, provider=provider)

    assert provider.models.count(AGENT_MODEL) == TURNS_BEFORE_THE_CEILING


def test_a_run_stopped_at_its_ceiling_records_it_in_its_scores(tmp_path: Path) -> None:
    scores = _run_under_a_dead_provider(_with_ceiling(PASSED_CEILING_SECONDS), tmp_path)

    assert scores["wall_clock_ceiling"] == {
        "seconds": PASSED_CEILING_SECONDS,
        "episode_stopped": True,
        "calibration_stopped": True,
    }


def test_a_run_under_a_dead_provider_scores_its_failed_model_calls(tmp_path: Path) -> None:
    scores = _run_under_a_dead_provider(LIVE, tmp_path)

    assert scores["failed_model_calls"] == {
        "episode": FAILED_CALLS_PER_EPISODE,
        "calibration": FAILED_CALLS_PER_EPISODE,
    }
