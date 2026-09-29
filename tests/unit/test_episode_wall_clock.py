"""A model-policy episode takes no turn past its wall-clock ceiling, so a dead provider cannot stretch it."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.gateway.core import Message, ProviderResult, ToolSpec
from loc_arena.gateway.openrouter_provider import ProviderError
from loc_arena.gateway.wiring import InProcessServices, open_episode_wiring
from loc_arena.live import run_model_policy
from loc_arena.scaffold.clock import SimulatedClock

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
AGENT_MODEL = CONFIG.models["untrusted_agent"].model
PASSED_CEILING_SECONDS = 0  # the ceiling has passed before the first turn


class FailingProvider:
    """The provider boundary, failing every call as OpenRouter does once its retries are spent."""

    def __init__(self) -> None:
        """Count the calls each model gets."""
        self.models: list[str] = []

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        """Fail the call."""
        self.models.append(model)
        raise ProviderError(f"{model}: the provider kept answering 503")


def test_an_episode_past_its_wall_clock_ceiling_takes_no_turn(tmp_path: Path) -> None:
    episode = dataclasses.replace(CONFIG.episode, wall_clock_seconds=PASSED_CEILING_SECONDS)
    config = dataclasses.replace(CONFIG, policy="model", episode=episode)
    provider = FailingProvider()
    services = InProcessServices(
        sealed_path=tmp_path / "sealed.jsonl",
        mirror_path=tmp_path / "mirror.jsonl",
        checkout=tmp_path / "checkout",
        provider=provider,
    )
    wiring = open_episode_wiring(config, services)
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
