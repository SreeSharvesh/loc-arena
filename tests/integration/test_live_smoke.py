from __future__ import annotations

from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.gateway.core import GatewayCore, GenerateRequest
from loc_arena.gateway.edge import GatewayEdge
from loc_arena.gateway.openrouter_provider import OpenRouterProvider
from loc_arena.logging_.events import AppendOnlyLog, read_events

EP = "ep-live"


def test_live_edge_to_core_one_real_call_one_sealed_record(
    tmp_path: Path,
    live_provider: OpenRouterProvider,
) -> None:
    cfg = load_run_config("configs/aurora-efficiency.deterministic.yaml")
    sealed_path = tmp_path / "sealed.jsonl"
    core = GatewayCore(cfg, EP, AppendOnlyLog(sealed_path, EP), live_provider, turn_secret="s")
    edge = GatewayEdge(EP, core, AppendOnlyLog(tmp_path / "mirror.jsonl", EP))
    resp = edge.generate(
        GenerateRequest(
            prompt="Reply with the single word: pong.",
            caller_identity="agent-main/distill-agent",
            role="untrusted_agent",
        ),
    )
    assert resp.text.strip()  # a real completion came back
    sealed = [e for e in read_events(sealed_path) if e.kind == "inference_call"]
    assert len(sealed) == 1  # exactly one sealed record for one call
    assert sealed[0].payload["turn_id"] is None and sealed[0].payload["batch_id"] is None
