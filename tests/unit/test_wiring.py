"""``open_episode_wiring``: the same ``EpisodeWiring`` in process and over the stack's services."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from loc_arena.config import RunConfig, load_run_config
from loc_arena.execution.workspace import SHELL_DISABLED_ERROR
from loc_arena.gateway.client import GatewayCallError
from loc_arena.gateway.core import DeterministicProvider, Message, Provider, ProviderResult, ToolSpec
from loc_arena.gateway.openrouter_provider import ProviderError
from loc_arena.gateway.wiring import EpisodeWiringError, InProcessServices, open_episode_wiring
from loc_arena.logging_.agent_trace import AgentTrace, TurnRef
from loc_arena.logging_.events import EventDraft
from loc_arena.stack.constants import MONITOR_MODEL_ROLE
from loc_arena.stack.contracts import CodeToolCall, EpisodeWiring, GenerateRequest

from tests.integration._gateway_support import JsonVerdictProvider
from tests.unit._stack_services import serve_stack

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
LIVE = dataclasses.replace(CONFIG, policy="model")
DRAFT = EventDraft(ts=1.0, actor_uid="agent-main", actor_role="orchestrator", kind="message")
CALL = GenerateRequest(prompt="plan", caller_identity="agent-main", role="untrusted_agent")
BASH = CodeToolCall(tool="bash", arguments={"command": "echo from-the-shell"})


class FailingProvider:
    """The provider boundary, failing every call as OpenRouter does once its retries are spent."""

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        raise ProviderError(f"{model}: the provider kept answering 503")


def _in_process(
    tmp_path: Path,
    config: RunConfig = CONFIG,
    *,
    provider: Provider | None = None,
    monitor_provider: Provider | None = None,
    trace: AgentTrace | None = None,
) -> EpisodeWiring:
    services = InProcessServices(
        sealed_path=tmp_path / "sealed.jsonl",
        mirror_path=tmp_path / "mirror.jsonl",
        checkout=tmp_path / "checkout",
        provider=provider or DeterministicProvider(),
        monitor_provider=monitor_provider,
    )
    return open_episode_wiring(config, services, trace=trace)


def test_a_scripted_episode_is_named_after_the_run_and_its_mode(tmp_path: Path) -> None:
    wiring = _in_process(tmp_path)

    event = wiring.sealed.record(DRAFT)

    assert event.episode_id == f"{CONFIG.run_slug}-attack"


def test_a_live_policy_episode_is_named_with_the_model_marker(tmp_path: Path) -> None:
    wiring = _in_process(tmp_path, LIVE)

    event = wiring.sealed.record(DRAFT)

    assert event.episode_id == f"{CONFIG.run_slug}-model-attack"


def test_a_provider_failure_in_process_raises_a_gateway_call_error(tmp_path: Path) -> None:
    wiring = _in_process(tmp_path, provider=FailingProvider())

    with pytest.raises(GatewayCallError, match="503"):
        wiring.gateway.generate(CALL)


def test_a_provider_failure_behind_the_edge_raises_a_gateway_call_error(tmp_path: Path) -> None:
    wiring = open_episode_wiring(LIVE, serve_stack(tmp_path, LIVE, provider=FailingProvider()).services)

    with pytest.raises(GatewayCallError, match="502"):
        wiring.gateway.generate(CALL)


def test_a_live_policy_needs_a_core_that_holds_a_provider_key(tmp_path: Path) -> None:
    services = serve_stack(tmp_path, LIVE, provider=None).services

    with pytest.raises(EpisodeWiringError, match="no provider key"):
        open_episode_wiring(LIVE, services)


def test_the_stack_wiring_opens_every_agents_sandbox_for_the_episode(tmp_path: Path) -> None:
    wiring = open_episode_wiring(CONFIG, serve_stack(tmp_path, CONFIG).services)

    results = [
        runner.run(CodeToolCall(tool="list_dir", arguments={})) for runner in wiring.code_tools.values()
    ]

    assert [result.result.get("error") for result in results] == [None] * len(CONFIG.agents)


def test_the_monitor_provider_answers_the_monitor_models_calls(tmp_path: Path) -> None:
    wiring = _in_process(tmp_path, monitor_provider=JsonVerdictProvider(suspicion=0.7))

    completion = wiring.control.call_model_as_monitor("action_monitor", "score this", 0.0)

    assert '"suspicion": 0.7' in completion


def test_the_agents_provider_answers_every_other_model_when_monitors_have_their_own(tmp_path: Path) -> None:
    wiring = _in_process(tmp_path, monitor_provider=JsonVerdictProvider())

    reply = wiring.gateway.generate(CALL)

    assert reply.text == "gen#1"


def test_a_monitor_model_that_also_serves_another_role_cannot_have_its_own_provider(tmp_path: Path) -> None:
    monitor_model = CONFIG.models[MONITOR_MODEL_ROLE].model
    teacher = dataclasses.replace(CONFIG.models["teacher"], model=monitor_model)
    shared = dataclasses.replace(CONFIG, models={**CONFIG.models, "teacher": teacher})

    with pytest.raises(EpisodeWiringError, match="also serves"):
        _in_process(tmp_path, shared, monitor_provider=JsonVerdictProvider())


def test_a_traced_model_call_belongs_to_the_turn_that_made_it(tmp_path: Path) -> None:
    trace = AgentTrace()
    wiring = _in_process(tmp_path, trace=trace)

    with trace.turn("agent-main", 4):
        reply = wiring.gateway.generate(CALL)

    assert trace.finish(last_sealed_seq=reply.seq).sealed_lane[reply.seq] == TurnRef("agent-main", 4)


def test_the_in_process_wiring_gives_agents_no_shell(tmp_path: Path) -> None:
    wiring = _in_process(tmp_path)

    result = wiring.code_tools["agent-main"].run(BASH).result

    assert result["error"] == SHELL_DISABLED_ERROR


def test_the_stack_wiring_runs_an_agents_shell_in_its_sandbox(tmp_path: Path) -> None:
    wiring = open_episode_wiring(CONFIG, serve_stack(tmp_path, CONFIG).services)

    result = wiring.code_tools["serving-agent"].run(BASH).result

    assert result["output"] == "from-the-shell\n"


def test_the_in_process_trace_keeps_the_exact_input_of_each_provider_call(tmp_path: Path) -> None:
    trace = AgentTrace()
    wiring = _in_process(tmp_path, trace=trace)

    reply = wiring.gateway.generate(CALL)

    (call,) = trace.finish(last_sealed_seq=reply.seq).model_calls
    assert call.model_input == f"{CONFIG.covert.objective_prompt}\n\nplan"
