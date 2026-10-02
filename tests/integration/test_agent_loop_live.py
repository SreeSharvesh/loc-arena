"""Live multi-turn loop against the development model, never the configured untrusted model."""

from __future__ import annotations

import copy
import os
from dataclasses import replace
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.gateway.client import GatewayClient
from loc_arena.gateway.core import (
    GatewayCore,
    Message,
    ProviderResult,
    ToolSpec,
    sign_turn_token,
)
from loc_arena.gateway.edge import GatewayEdge
from loc_arena.gateway.openrouter_provider import OpenRouterProvider
from loc_arena.logging_.events import AppendOnlyLog, read_events
from loc_arena.scaffold.model_policy import ModelAgentPolicy
from loc_arena.scaffold.tool_specs import agent_tool_specs

_DEV = os.environ.get("UNTRUSTED_MODEL_DEVELOPMENT", "")
_LIVE = os.environ.get("LOC_ARENA_LIVE") == "1" and bool(os.environ.get("OPENROUTER_API_KEY")) and bool(_DEV)
pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="set LOC_ARENA_LIVE=1, OPENROUTER_API_KEY, and UNTRUSTED_MODEL_DEVELOPMENT",
)

EP = "ep-agent-loop-live"
SECRET = "s"
_SYSTEM = "Call list_dir with path '.' once, then stop. On the next turn call finish."


class _RecordingProvider:
    """Records each model id and message list, then forwards to OpenRouter."""

    def __init__(self, inner: OpenRouterProvider) -> None:
        """Hold the real provider and an empty call log."""
        self._inner = inner
        self.calls: list[tuple[str, list[Message]]] = []

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        """Record the request and return the provider's completion."""
        self.calls.append((model, copy.deepcopy(messages)))
        return self._inner.generate(model, messages, temperature, max_tokens, tools)


def test_dev_model_keeps_a_tool_result_for_the_next_turn(
    tmp_path: Path,
    live_provider: OpenRouterProvider,
) -> None:
    cfg = load_run_config("configs/aurora-efficiency.deterministic.yaml")
    model_id = _DEV.removeprefix("openrouter/")
    cfg.models["untrusted_agent"] = replace(cfg.models["untrusted_agent"], model=model_id)
    provider = _RecordingProvider(live_provider)
    sealed_path = tmp_path / "sealed.jsonl"
    mirror_path = tmp_path / "mirror.jsonl"
    core = GatewayCore(cfg, EP, AppendOnlyLog(sealed_path, EP), provider, turn_secret=SECRET)
    edge = GatewayEdge(EP, core, AppendOnlyLog(mirror_path, EP))
    client = GatewayClient(edge, "serving-agent")
    client.set_turn_token(sign_turn_token(SECRET, EP, "serving-agent", turn=0, exp=1e12))
    policy = ModelAgentPolicy(
        client,
        _SYSTEM,
        agent_tool_specs(covert=False),
        cfg.agent_loop,
    )

    transcript: list[dict[str, object]] = []
    for turn in range(4):
        actions = policy.next_actions("serving-agent", turn, transcript) or []
        calls = [action for action in actions if action.call_id and action.tool != "finish"]
        for action in calls:
            transcript.append({"call_id": action.call_id, "result": {"content": "a.py\n"}})
        if calls:
            policy.next_actions("serving-agent", turn + 1, transcript)
            break
    else:
        pytest.fail("development model never returned a tool call")

    assert {model for model, _messages in provider.calls} == {model_id}
    tool_messages = [message for message in provider.calls[-1][1] if message["role"] == "tool"]
    assert tool_messages[0]["content"] == "a.py\n"
    sealed = [event for event in read_events(sealed_path) if event.kind == "inference_call"]
    mirror = [event for event in read_events(mirror_path) if event.kind == "inference_call"]
    assert sealed and all(event.payload["prompt_tokens"] > 0 for event in sealed)
    assert mirror and all("prompt_tokens" not in event.payload for event in mirror)
