from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.gateway.core import GatewayCore, GenerateRequest, OpenRouterProvider, create_core_app
from loc_arena.gateway.edge import GatewayEdge
from loc_arena.logging_.events import AppendOnlyLog, read_events

from tests.integration._gateway_support import AppTransport, StubProvider

EP = "ep-isolation"
CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")


def test_edge_holds_no_provider_or_key(tmp_path: Path) -> None:
    provider = StubProvider()
    core = GatewayCore(CFG, EP, AppendOnlyLog(tmp_path / "sealed.jsonl", EP), provider, turn_secret="s")
    edge = GatewayEdge(EP, AppTransport(create_core_app(core)), AppendOnlyLog(tmp_path / "mirror.jsonl", EP))
    # the edge references no provider and no sealed log; its only outbound is the transport to the core
    attr_values = list(vars(edge).values())
    assert provider not in attr_values
    assert not any(isinstance(v, OpenRouterProvider) for v in attr_values)
    assert not any("OPENROUTER" in str(v) for v in attr_values)


def test_provider_requires_key_to_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        OpenRouterProvider()


def test_only_core_writes_sealed_log(tmp_path: Path) -> None:
    provider = StubProvider()
    sealed_path = tmp_path / "sealed.jsonl"
    mirror_path = tmp_path / "mirror.jsonl"
    core = GatewayCore(CFG, EP, AppendOnlyLog(sealed_path, EP), provider, turn_secret="s")
    edge = GatewayEdge(EP, AppTransport(create_core_app(core)), AppendOnlyLog(mirror_path, EP))
    edge.generate(GenerateRequest(prompt="hi", caller_identity="agent-main/distill-agent"))
    sealed = [e for e in read_events(sealed_path) if e.kind == "inference_call"]
    mirror = [e for e in read_events(mirror_path) if e.kind == "inference_call"]
    assert len(sealed) == 1 and len(mirror) == 1  # one sealed (core) + one mirror (edge), distinct files
    assert sealed_path != mirror_path
    # NOTE: network/volume enforcement (the agent literally cannot reach sealed-net) is asserted in docker.
