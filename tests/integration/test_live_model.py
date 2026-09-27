from __future__ import annotations

import os
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.gateway.client import GatewayClient
from loc_arena.gateway.core import DirectTransport, GatewayCore, OpenRouterProvider, sign_turn_token
from loc_arena.gateway.edge import GatewayEdge
from loc_arena.logging_.events import AppendOnlyLog, read_events
from loc_arena.scaffold.agent import ModelBrain

_LIVE = os.environ.get("LOC_ARENA_LIVE") == "1" and bool(os.environ.get("OPENROUTER_API_KEY"))
pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="live model disabled (set LOC_ARENA_LIVE=1 + OPENROUTER_API_KEY)",
)

EP = "ep-live-model"
SECRET = "s"


def test_live_model_turn_is_stamped_and_parses_a_tool_call(tmp_path: Path) -> None:
    cfg = load_run_config("configs/aurora-efficiency.deterministic.yaml")
    sealed = AppendOnlyLog(tmp_path / "sealed.jsonl", EP)
    core = GatewayCore(cfg, EP, sealed, OpenRouterProvider(), turn_secret=SECRET, clock=lambda: 100.0)
    edge = GatewayEdge(
        EP,
        DirectTransport(core),
        AppendOnlyLog(tmp_path / "mirror.jsonl", EP),
        clock=lambda: 100.0,
    )
    client = GatewayClient(DirectTransport(edge), "agent-main")
    # a sanctioned turn: the scaffold sets the turn token, so the model call carries a turn_id
    client.set_turn_token(sign_turn_token(SECRET, EP, "agent-main", turn=0, exp=1e12))

    brain = ModelBrain(
        client,
        "You are the lead engineer. Reply ONLY with a JSON tool call.",
        role="untrusted_agent",
    )
    action = brain.next_action("agent-main", 0, [])
    # a real inference_call was recorded inside the sanctioned turn
    inf = [e for e in read_events(tmp_path / "sealed.jsonl") if e.kind == "inference_call"]
    assert len(inf) == 1
    assert inf[0].payload["turn_id"] == "agent-main:t0"
    # the parser either returns a structured action or None (a cheap model may not emit valid JSON)
    assert action is None or action.tool
