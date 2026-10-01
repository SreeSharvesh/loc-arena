"""A failed model call fails that turn, or that tool call, never the episode: in process and in the stack."""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.forge.forge import Forge
from loc_arena.forge.services import ForgeServices
from loc_arena.forge.world import generate_world
from loc_arena.gateway.client import GatewayClient
from loc_arena.gateway.core import Message, ProviderResult, ToolSpec
from loc_arena.gateway.openrouter_provider import ProviderError
from loc_arena.gateway.wiring import InProcessServices, open_episode_wiring
from loc_arena.logging_.events import read_events
from loc_arena.npcs.coworker import run_coworker
from loc_arena.scaffold.agent import Agent, TurnStatus
from loc_arena.scaffold.bus import MessageBus, Recorder
from loc_arena.scaffold.clock import SimulatedClock
from loc_arena.scaffold.covert_tools import CovertToolServices
from loc_arena.scaffold.model_policy import ModelAgentPolicy
from loc_arena.scaffold.registry import AgentRegistry
from loc_arena.scaffold.tool_specs import agent_tool_specs
from loc_arena.scaffold.tools import AgentAction, AgentContext, StubServices, ToolResult, Tools
from loc_arena.stack.contracts import EpisodeWiring
from scenarios.loader import load_scenario

from tests.unit._stack_services import serve_stack

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
LIVE = dataclasses.replace(CONFIG, policy="model")
TURNS = 6  # more turns than an agent may yield in a row before it is ended as a refuser


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


class ScriptedProvider:
    """The provider boundary, giving each call the next scripted reply; a ``ProviderError`` fails that call.

    It keeps the messages of every call it gets.
    """

    def __init__(self, replies: list[ProviderResult | ProviderError]) -> None:
        """Hold the replies, one per call, in order."""
        self.requests: list[list[Message]] = []
        self._replies = list(replies)

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        self.requests.append(list(messages))
        reply = self._replies.pop(0)
        if isinstance(reply, ProviderError):
            raise reply
        return reply


def _no_spawn(ctx: AgentContext, args: dict[str, object], turn: int) -> ToolResult:
    return {"blocked": True}


def _failing_wiring(tmp_path: Path) -> EpisodeWiring:
    return _in_process_wiring(tmp_path, FailingProvider())


def _in_process_wiring(tmp_path: Path, provider: FailingProvider | ScriptedProvider) -> EpisodeWiring:
    services = InProcessServices(
        sealed_path=tmp_path / "sealed.jsonl",
        mirror_path=tmp_path / "mirror.jsonl",
        checkout=tmp_path / "checkout",
        provider=provider,
    )
    return open_episode_wiring(CONFIG, services)


def _context(wiring: EpisodeWiring, agent_id: str) -> AgentContext:
    agent = CONFIG.agent(agent_id)
    client = GatewayClient(wiring.gateway, agent.id)
    return AgentContext(uid=agent.id, role=agent.kind, branch=agent.branch, scope=agent.scope, client=client)


def _model_agent(wiring: EpisodeWiring) -> Agent:
    clock = SimulatedClock(wiring.control, CONFIG.settings.clock.start_ts)
    recorder = Recorder(wiring.sealed, wiring.mirror, clock=clock)
    bus = MessageBus(recorder)
    ctx = _context(wiring, "agent-main")
    registry = AgentRegistry(
        CONFIG.episode,
        recorder,
        None,
        root_uid=ctx.uid,
        root_role=ctx.role,
        root_branch=ctx.branch,
        root_scope=ctx.scope,
        clock=clock,
    )
    tools = Tools(recorder, bus, StubServices(), spawn_handler=_no_spawn)
    return Agent(
        ctx,
        ModelAgentPolicy(ctx.client, "brief", agent_tool_specs(covert=False), CONFIG.agent_loop),
        tools,
        registry,
        bus,
        wiring.control,
        TURNS,
        clock=clock,
    )


def test_a_turn_whose_model_call_fails_leaves_the_agent_running(tmp_path: Path) -> None:
    agent = _model_agent(_failing_wiring(tmp_path))

    status = agent.run_turn()

    assert status is TurnStatus.CONTINUE


def test_a_turn_whose_model_call_the_edge_answers_with_502_leaves_the_agent_running(tmp_path: Path) -> None:
    agent = _model_agent(
        open_episode_wiring(LIVE, serve_stack(tmp_path, LIVE, provider=FailingProvider()).services),
    )

    status = agent.run_turn()

    assert status is TurnStatus.CONTINUE


def test_a_failed_turn_is_marked_failed_in_the_agents_transcript(tmp_path: Path) -> None:
    agent = _model_agent(_failing_wiring(tmp_path))

    agent.run_turn()

    assert agent.transcript == [{"turn": 0, "failed": "the model call failed"}]


def test_a_failed_turn_is_logged_as_failed(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    agent = _model_agent(_failing_wiring(tmp_path))

    with caplog.at_level(logging.WARNING):
        agent.run_turn()

    assert "agent-main turn 0 failed: its model call failed" in caplog.text


def test_failed_turns_do_not_count_as_refusals(tmp_path: Path) -> None:
    agent = _model_agent(_failing_wiring(tmp_path))

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
    agent = _model_agent(_in_process_wiring(tmp_path, provider))
    agent.run_turn()  # its tool call runs, so the next turn's history carries the result
    agent.run_turn()  # its model call fails

    agent.run_turn()

    assert provider.requests[2] == provider.requests[1]


def test_a_failed_turn_leaves_the_count_of_skipped_turns_as_it_was(tmp_path: Path) -> None:
    text = ProviderResult(text="thinking", prompt_tokens=1, completion_tokens=1)  # no tool call: a skip
    failure = ProviderError("the provider kept answering 503")
    agent = _model_agent(_in_process_wiring(tmp_path, ScriptedProvider([text, failure, text, text, text])))

    statuses = [agent.run_turn() for _ in range(5)]

    assert statuses == [TurnStatus.CONTINUE] * 4 + [TurnStatus.ENDED]  # the fourth skip ends the agent


def test_a_generate_tool_whose_call_fails_returns_an_error_result(tmp_path: Path) -> None:
    wiring = _failing_wiring(tmp_path)
    recorder = Recorder(wiring.sealed, wiring.mirror, clock=lambda: 0.0)
    tools = Tools(recorder, MessageBus(recorder), StubServices(), spawn_handler=_no_spawn)

    result = tools.execute(_context(wiring, "agent-main"), AgentAction("generate", {"prompt": "draft"}), 0)

    assert "a model call failed" in str(result["error"])


def test_the_rogue_loop_tool_returns_an_error_result_when_its_calls_fail(tmp_path: Path) -> None:
    wiring = _failing_wiring(tmp_path)
    clock = SimulatedClock(wiring.control, CONFIG.settings.clock.start_ts)
    recorder = Recorder(wiring.sealed, wiring.mirror, clock=clock)
    world = generate_world(CONFIG, load_scenario("aurora_efficiency"), CONFIG.seed)
    covert = CovertToolServices(
        ForgeServices(Forge(world)),
        wiring=wiring,
        recorder=recorder,
        clock=clock,
        deadline_ts=CONFIG.settings.clock.deadline_ts,
        agents=[agent.id for agent in CONFIG.agents],
        rogue_account="batch-runner",
    )
    tools = Tools(recorder, MessageBus(recorder), covert, spawn_handler=_no_spawn)

    result = tools.execute(_context(wiring, "serving-agent"), AgentAction("start_rogue_loop", {}), 0)

    assert "a model call failed" in str(result["error"])


def test_a_coworker_batch_the_provider_fails_is_skipped_without_ending_the_episode(tmp_path: Path) -> None:
    wiring = _failing_wiring(tmp_path)
    clock = SimulatedClock(wiring.control, CONFIG.settings.clock.start_ts)

    run_coworker(wiring.gateway, Recorder(wiring.sealed, wiring.mirror, clock=clock), clock)

    assert [event.kind for event in read_events(tmp_path / "sealed.jsonl")] == ["job"]
