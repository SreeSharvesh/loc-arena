"""A model-policy episode takes no turn past its wall-clock ceiling, so a dead provider cannot stretch it."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.live import run_model_policy
from loc_arena.scaffold.clock import SimulatedClock

from tests.integration._gateway_support import FailingProvider
from tests.unit._stack_services import open_in_process

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
AGENT_MODEL = CONFIG.models["untrusted_agent"].model
PASSED_CEILING_SECONDS = 0  # the ceiling has passed before the first turn


def test_an_episode_past_its_wall_clock_ceiling_takes_no_turn(tmp_path: Path) -> None:
    episode = dataclasses.replace(CONFIG.episode, wall_clock_seconds=PASSED_CEILING_SECONDS)
    config = dataclasses.replace(CONFIG, policy="model", episode=episode)
    provider = FailingProvider()
    wiring = open_in_process(tmp_path, config, provider=provider)
    clock = SimulatedClock(wiring.control, config.settings.clock.start_ts)

    run_model_policy(
        config,
        wiring,
        clock,
        None,
        deadline_ts=config.settings.clock.deadline_ts,
        shell_offered=False,
    )

    assert AGENT_MODEL not in provider.models
