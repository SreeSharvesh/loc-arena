from __future__ import annotations

from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.gateway.core import BatchGenerateRequest, DeterministicProvider, GatewayCore, GenerateRequest
from loc_arena.harness import apply_mode
from loc_arena.logging_.agent_trace import AgentTrace, TurnRef
from loc_arena.logging_.events import AppendOnlyLog

ATTACK = apply_mode(load_run_config("configs/aurora-efficiency.deterministic.yaml"), "attack")


def _core(tmp_path: Path, trace: AgentTrace) -> GatewayCore:
    sealed = AppendOnlyLog(tmp_path / "sealed.jsonl", "ep-core", on_append=trace.on_sealed_append)
    return GatewayCore(ATTACK, "ep-core", sealed, DeterministicProvider(), turn_secret="s", trace=trace)


def test_generate_reports_the_post_injection_input_at_its_sealed_seq(tmp_path: Path) -> None:
    trace = AgentTrace()
    core = _core(tmp_path, trace)
    target = ATTACK.covert.target_identity
    with trace.turn(target, 0):
        response = core.generate(
            GenerateRequest(prompt="plan", caller_identity=target, role="untrusted_agent")
        )
    finished = trace.finish(last_sealed_seq=response.seq)
    (call,) = finished.model_calls
    assert call.model_input == f"{ATTACK.covert.objective_prompt}\n\nplan"
    assert (call.identity, call.output, call.sealed_seq) == (target, response.text, response.seq)
    assert finished.sealed_lane[call.sealed_seq] == TurnRef(target, 0)


def test_batch_generate_reports_one_call_per_prompt_outside_any_turn(tmp_path: Path) -> None:
    trace = AgentTrace()
    core = _core(tmp_path, trace)
    response = core.batch_generate(BatchGenerateRequest(prompts=["a", "b"], caller_identity="coworker"))
    calls = trace.finish(last_sealed_seq=response.seqs[-1]).model_calls
    assert [(c.model_input, c.sealed_seq, c.phase) for c in calls] == [
        ("a", response.seqs[0], None),
        ("b", response.seqs[1], None),
    ]
