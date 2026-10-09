"""A model-policy run ends: no turn starts past the wall-clock ceiling, and play survives a dead provider."""

from __future__ import annotations

import dataclasses
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
PASSED_CEILING_SECONDS = 0  # the ceiling has passed before the first turn
STOPPED = dataclasses.replace(
    LIVE,
    episode=dataclasses.replace(LIVE.episode, wall_clock_seconds=PASSED_CEILING_SECONDS),
)


@pytest.fixture(autouse=True)
def no_live_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(API_KEY_VARIABLE, raising=False)
    monkeypatch.delenv(GATEWAY_URL_VARIABLE, raising=False)


def test_an_episode_past_its_wall_clock_ceiling_starts_no_agent_turn(tmp_path: Path) -> None:
    provider = FailingProvider()

    play_model_episode(STOPPED, tmp_path, provider=provider)

    assert AGENT_MODEL not in provider.models


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (STOPPED, {"seconds": PASSED_CEILING_SECONDS, "episode_stopped": True, "calibration_stopped": True}),
        (LIVE, None),
    ],
    ids=["stopped at the ceiling", "ended before the ceiling"],
)
def test_a_run_under_a_dead_provider_scores_whether_the_ceiling_stopped_it(
    config: RunConfig,
    expected: dict[str, object] | None,
    tmp_path: Path,
) -> None:
    bundle = run_episode(
        config,
        mode="attack",
        out_root=tmp_path,
        provider=FailingProvider(),
        calibration_provider=FailingProvider(),
        write_report=False,
    )

    assert json.loads((bundle / "scores.json").read_text()).get("wall_clock_ceiling") == expected
