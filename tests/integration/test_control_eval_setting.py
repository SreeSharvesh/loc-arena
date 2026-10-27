"""Every agent is told it plays in an AI control evaluation, in the attack play and its honest twin alike."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from loc_arena.config import load_run_config
from loc_arena.gateway.core import Message, ProviderResult, ScriptedProvider
from loc_arena.harness import apply_mode
from loc_arena.live import CONTROL_EVAL_DOC, play_model_episode

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")


class RecordingProvider(ScriptedProvider):
    """Finishes every agent at once, keeping the system prompt of each call."""

    def __init__(self) -> None:
        """Script no moves, so each agent finishes on its first turn."""
        super().__init__({})
        self.system_prompts: list[str] = []

    def generate(self, model: str, messages: list[Message], *args: Any, **kwargs: Any) -> ProviderResult:  # noqa: ANN401 - passed through
        self.system_prompts.append(str(messages[0]["content"]))
        return super().generate(model, messages, *args, **kwargs)


@pytest.mark.parametrize("mode", ["attack", "honest"])
def test_every_agent_is_told_it_plays_in_a_control_evaluation(tmp_path: Path, mode: str) -> None:
    provider = RecordingProvider()

    play_model_episode(apply_mode(CFG, mode), tmp_path, provider=provider, deadline_ts=10_000.0)

    told = {
        re.findall(r"You are (\S+)\.", prompt)[0]
        for prompt in provider.system_prompts
        if CONTROL_EVAL_DOC in prompt
    }
    assert told == {agent.id for agent in CFG.agents}
