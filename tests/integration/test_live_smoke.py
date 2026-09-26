from __future__ import annotations

import os
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.gateway.core import GatewayCore, GenerateRequest, OpenRouterProvider, create_core_app
from loc_arena.gateway.edge import GatewayEdge
from loc_arena.logging_.events import AppendOnlyLog, read_events

from tests.integration._gateway_support import AppTransport

_LIVE = os.environ.get("LOC_ARENA_LIVE") == "1" and bool(os.environ.get("OPENROUTER_API_KEY"))
pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="live provider disabled (set LOC_ARENA_LIVE=1 + OPENROUTER_API_KEY)",
)

EP = "ep-live"


def test_live_edge_to_core_one_real_call_one_sealed_record(tmp_path: Path) -> None:
    cfg = load_run_config("configs/aurora-efficiency.deterministic.yaml")
    sealed_path = tmp_path / "sealed.jsonl"
    core = GatewayCore(cfg, EP, AppendOnlyLog(sealed_path, EP), OpenRouterProvider(), turn_secret="s")
    edge = GatewayEdge(EP, AppTransport(create_core_app(core)), AppendOnlyLog(tmp_path / "mirror.jsonl", EP))
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
