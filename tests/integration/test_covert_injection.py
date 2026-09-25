from __future__ import annotations

import dataclasses
from pathlib import Path

from loc_arena.config import RunConfig, load_run_config
from loc_arena.gateway.core import GatewayCore, GenerateRequest, create_core_app
from loc_arena.gateway.edge import GatewayEdge
from loc_arena.logging_.events import AppendOnlyLog, fingerprint, read_events

from tests.integration._gateway_support import AppTransport, StubProvider

EP = "ep-covert"
SECRET = "s"
CFG = load_run_config(
    "configs/aurora-efficiency.deterministic.yaml",
)  # covert.enabled = True, target = agent-main
COVERT = CFG.covert.objective_prompt


def _wire(tmp_path: Path, config: RunConfig = CFG) -> tuple[StubProvider, GatewayEdge, Path]:
    provider = StubProvider()
    sealed = AppendOnlyLog(tmp_path / "sealed.jsonl", EP)
    core = GatewayCore(config, EP, sealed, provider, turn_secret=SECRET, clock=lambda: 1.0)
    edge = GatewayEdge(
        EP,
        AppTransport(create_core_app(core)),
        AppendOnlyLog(tmp_path / "mirror.jsonl", EP),
        clock=lambda: 1.0,
    )
    return provider, edge, tmp_path


def test_covert_reaches_model_for_target_only(tmp_path: Path) -> None:
    provider, edge, _ = _wire(tmp_path)
    edge.generate(GenerateRequest(prompt="do the work", caller_identity="agent-main"))
    edge.generate(GenerateRequest(prompt="do the work", caller_identity="agent-main/distill-agent"))
    # the model input for agent-main carries the covert objective; the sub-agent's does not
    assert any(COVERT in inp for inp in provider.inputs)
    target_input = provider.inputs[0]
    other_input = provider.inputs[1]
    assert COVERT in target_input
    assert COVERT not in other_input


def test_covert_never_in_mirror_record(tmp_path: Path) -> None:
    _, edge, tp = _wire(tmp_path)
    edge.generate(GenerateRequest(prompt="do the work", caller_identity="agent-main"))
    mirror = [e for e in read_events(tp / "mirror.jsonl") if e.kind == "inference_call"]
    (rec,) = mirror
    # the mirror keys off the ORIGINAL prompt, not the injected one, so covert text is unrecoverable there
    assert rec.payload["prompt_fp"] == fingerprint("do the work")
    assert rec.payload["prompt_fp"] != fingerprint(f"{COVERT}\n\ndo the work")


def test_no_injection_when_disabled(tmp_path: Path) -> None:
    honest = dataclasses.replace(CFG, covert=dataclasses.replace(CFG.covert, enabled=False))
    provider, edge, _ = _wire(tmp_path, config=honest)
    edge.generate(GenerateRequest(prompt="do the work", caller_identity="agent-main"))
    assert all(COVERT not in inp for inp in provider.inputs)
    assert provider.inputs == ["do the work"]
