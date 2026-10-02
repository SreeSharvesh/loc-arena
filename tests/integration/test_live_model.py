from __future__ import annotations

from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.gateway.client import GatewayClient
from loc_arena.gateway.core import DirectTransport, GatewayCore, sign_turn_token
from loc_arena.gateway.edge import GatewayEdge
from loc_arena.gateway.openrouter_provider import OpenRouterProvider
from loc_arena.logging_.events import AppendOnlyLog, read_events
from loc_arena.scaffold.model_policy import ModelAgentPolicy
from loc_arena.scaffold.tool_specs import agent_tool_specs

EP = "ep-live-model"
SECRET = "s"


def test_live_model_turn_is_stamped_and_parses_a_tool_call(
    tmp_path: Path,
    live_provider: OpenRouterProvider,
) -> None:
    cfg = load_run_config("configs/aurora-efficiency.deterministic.yaml")
    sealed = AppendOnlyLog(tmp_path / "sealed.jsonl", EP)
    core = GatewayCore(cfg, EP, sealed, live_provider, turn_secret=SECRET, clock=lambda: 100.0)
    edge = GatewayEdge(
        EP,
        DirectTransport(core),
        AppendOnlyLog(tmp_path / "mirror.jsonl", EP),
        clock=lambda: 100.0,
    )
    client = GatewayClient(DirectTransport(edge), "agent-main")
    # a sanctioned turn: the scaffold sets the turn token, so the model call carries a turn_id
    client.set_turn_token(sign_turn_token(SECRET, EP, "agent-main", turn=0, exp=1e12))

    agent_policy = ModelAgentPolicy(
        client,
        "You are the lead engineer. Use a tool.",
        agent_tool_specs(covert=False),
        cfg.agent_loop,
    )
    actions = agent_policy.next_actions("agent-main", 0, [])
    # a real inference_call was recorded inside the sanctioned turn
    inf = [e for e in read_events(tmp_path / "sealed.jsonl") if e.kind == "inference_call"]
    assert len(inf) == 1
    assert inf[0].payload["turn_id"] == "agent-main:t0"
    assert actions is not None and actions[0].tool
