"""A failed model call fails only that turn or that tool call, in process and in the stack."""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.gateway.core import ProviderResult
from loc_arena.gateway.openrouter_provider import ProviderError
from loc_arena.gateway.wiring import open_episode_wiring
from loc_arena.logging_.events import read_events
from loc_arena.npcs.coworker import run_coworker
from loc_arena.scaffold.agent import Agent, TurnStatus
from loc_arena.scaffold.bus import MessageBus, Recorder
from loc_arena.scaffold.clock import SimulatedClock
from loc_arena.scaffold.tools import AgentAction, StubServices, Tools
from loc_arena.stack.contracts import EpisodeWiring

from tests.integration._gateway_support import FailingProvider, ScriptedProvider
from tests.unit._openrouter_stub import (
    LONE_SURROGATE,
    ScriptedReply,
    StubOpenRouter,
    completion,
    serve_openrouter,
    stub_provider,
)
from tests.unit._stack_services import (
    agent_context,
    covert_tools,
    model_agent,
    no_spawn,
    open_in_process,
    serve_stack,
)

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
LIVE = dataclasses.replace(CONFIG, policy="model")
TURNS = 6  # more turns than an agent may yield in a row before it is ended as a refuser


def _failing_wiring(tmp_path: Path) -> EpisodeWiring:
    return open_in_process(tmp_path, CONFIG, provider=FailingProvider())


def test_a_turn_whose_model_call_fails_leaves_the_agent_running(tmp_path: Path) -> None:
    agent = model_agent(CONFIG, _failing_wiring(tmp_path), TURNS)

    status = agent.run_turn()

    assert status is TurnStatus.CONTINUE


def test_a_turn_whose_model_call_the_edge_answers_with_502_leaves_the_agent_running(tmp_path: Path) -> None:
    agent = model_agent(
        CONFIG,
        open_episode_wiring(LIVE, serve_stack(tmp_path, LIVE, provider=FailingProvider()).services),
        TURNS,
    )

    status = agent.run_turn()

    assert status is TurnStatus.CONTINUE


def test_a_failed_turn_is_marked_failed_in_the_agents_transcript(tmp_path: Path) -> None:
    agent = model_agent(CONFIG, _failing_wiring(tmp_path), TURNS)

    agent.run_turn()

    assert agent.transcript == [{"turn": 0, "failed": "the model call failed"}]


def test_a_failed_turn_is_logged_as_failed(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    agent = model_agent(CONFIG, _failing_wiring(tmp_path), TURNS)

    with caplog.at_level(logging.WARNING):
        agent.run_turn()

    assert "agent-main turn 0 failed: its model call failed" in caplog.text


def test_failed_turns_do_not_count_as_refusals(tmp_path: Path) -> None:
    agent = model_agent(CONFIG, _failing_wiring(tmp_path), TURNS)

    statuses = [agent.run_turn() for _ in range(TURNS)]

    assert statuses == [TurnStatus.CONTINUE] * TURNS


def test_a_failed_model_call_adds_nothing_to_the_history_the_next_turn_sends(tmp_path: Path) -> None:
    tool_call = {"id": "call-1", "type": "function", "function": {"name": "run_benchmark", "arguments": "{}"}}
    provider = ScriptedProvider(
        [
            ProviderResult(text="", prompt_tokens=1, completion_tokens=1, tool_calls=[tool_call]),
            ProviderError("the provider kept answering 503"),
            ProviderResult(text="thinking", prompt_tokens=1, completion_tokens=1),
        ],
    )
    agent = model_agent(CONFIG, open_in_process(tmp_path, CONFIG, provider=provider), TURNS)
    agent.run_turn()  # its tool call runs, so the next turn's history carries the result
    agent.run_turn()  # its model call fails

    agent.run_turn()

    assert provider.requests[2] == provider.requests[1]


def test_a_failed_turn_leaves_the_count_of_skipped_turns_as_it_was(tmp_path: Path) -> None:
    text = ProviderResult(text="thinking", prompt_tokens=1, completion_tokens=1)  # no tool call: a skip
    failure = ProviderError("the provider kept answering 503")
    provider = ScriptedProvider([text, failure, text, text, text])
    agent = model_agent(CONFIG, open_in_process(tmp_path, CONFIG, provider=provider), TURNS)

    statuses = [agent.run_turn() for _ in range(5)]

    assert statuses == [TurnStatus.CONTINUE] * 4 + [TurnStatus.ENDED]  # the fourth skip ends the agent


def test_a_generate_tool_whose_call_fails_returns_an_error_result(tmp_path: Path) -> None:
    wiring = _failing_wiring(tmp_path)
    recorder = Recorder(wiring.sealed, wiring.mirror, clock=lambda: 0.0)
    tools = Tools(recorder, MessageBus(recorder), StubServices(), spawn_handler=no_spawn)
    ctx = agent_context(CONFIG, wiring, "agent-main")

    result = tools.execute(ctx, AgentAction("generate", {"prompt": "draft"}), 0)

    assert "a model call failed" in str(result["error"])


def test_the_rogue_loop_tool_returns_an_error_result_when_its_calls_fail(tmp_path: Path) -> None:
    wiring = _failing_wiring(tmp_path)
    tools = covert_tools(CONFIG, wiring)
    ctx = agent_context(CONFIG, wiring, "serving-agent")

    result = tools.execute(ctx, AgentAction("start_rogue_loop", {}), 0)

    assert "a model call failed" in str(result["error"])


def test_a_coworker_batch_the_provider_fails_is_skipped_without_ending_the_episode(tmp_path: Path) -> None:
    wiring = _failing_wiring(tmp_path)
    clock = SimulatedClock(wiring.control, CONFIG.settings.clock.start_ts)

    run_coworker(wiring.gateway, Recorder(wiring.sealed, wiring.mirror, clock=clock), clock)

    assert [event.kind for event in read_events(tmp_path / "sealed.jsonl")] == ["job", "inference_error"]


def _kinds(log: Path) -> list[str]:
    return [event.kind for event in read_events(log)] if log.exists() else []


def _sdk_agent(tmp_path: Path, stub: StubOpenRouter) -> Agent:
    return model_agent(CONFIG, open_in_process(tmp_path, CONFIG, provider=stub_provider(stub)), TURNS)


def test_a_reply_of_content_parts_is_sealed_as_a_failed_call_and_never_mirrored(tmp_path: Path) -> None:
    reply = json.loads(completion())
    reply["choices"][0]["message"]["content"] = [{"type": "text", "text": "thinking"}]
    with serve_openrouter(ScriptedReply(body=json.dumps(reply))) as stub:
        agent = _sdk_agent(tmp_path, stub)

        agent.run_turn()

    assert (_kinds(tmp_path / "sealed.jsonl"), _kinds(tmp_path / "mirror.jsonl")) == (["inference_error"], [])


def test_a_tool_call_holding_a_lone_surrogate_is_sealed_as_a_failed_call_and_never_mirrored(
    tmp_path: Path,
) -> None:
    function = {"name": "list_dir", "arguments": '{"path": "' + LONE_SURROGATE + '"}'}
    body = completion(tool_calls=[{"id": "call-1", "type": "function", "function": function}])
    with serve_openrouter(ScriptedReply(body=body)) as stub:
        agent = _sdk_agent(tmp_path, stub)

        agent.run_turn()

    assert (_kinds(tmp_path / "sealed.jsonl"), _kinds(tmp_path / "mirror.jsonl")) == (["inference_error"], [])
