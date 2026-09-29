"""A model reply the scaffold cannot act on costs the agent that turn, never the episode.

Each agent test gives one agent a provider that answers every call with the same native tool-call reply, as a
live model can, and runs its turn through the real agent policy, tool layer and event logs. The tool specs
offer no batch_generate, so its test calls the tool layer directly, as a scripted agent policy can.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.gateway.client import GatewayClient
from loc_arena.gateway.core import Message, ProviderResult, ToolSpec
from loc_arena.gateway.wiring import InProcessServices, open_episode_wiring
from loc_arena.scaffold.agent import Agent
from loc_arena.scaffold.bus import MessageBus, Recorder
from loc_arena.scaffold.clock import SimulatedClock
from loc_arena.scaffold.model_policy import ModelAgentPolicy
from loc_arena.scaffold.registry import AgentRegistry
from loc_arena.scaffold.tool_specs import agent_tool_specs
from loc_arena.scaffold.tools import AgentAction, AgentContext, StubServices, ToolResult, Tools
from loc_arena.stack.contracts import EpisodeWiring

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
SETTINGS = CONFIG.settings
AGENT_ID = "agent-main"
TURNS = 3
TOKENS = 1
CALL_ID = "call-1"
SKIPPED_FIRST_TURN = [{"turn": 0, "skipped": True}]
# Far past the depth CPython's JSON decoder follows before it raises RecursionError.
DECODER_DEFEATING_DEPTH = 100_000
# Well past the limit, and past the ~490 levels at which recording the action once hit RecursionError.
DEPTH_PAST_THE_LIMIT_FACTOR = 20


class ReplyingProvider:
    """The provider boundary, answering every call with one native tool call."""

    def __init__(self, tool: str, arguments: str, *, call_id: str = CALL_ID) -> None:
        """Answer every call with a call of ``tool`` under ``call_id``, its arguments the JSON text given."""
        self._tool = tool
        self._arguments = arguments
        self._call_id = call_id
        self.requests: list[list[Message]] = []

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        """The fixed tool call, built afresh for each call; records the messages the call carried."""
        self.requests.append(copy.deepcopy(messages))
        call = {
            "id": self._call_id,
            "type": "function",
            "function": {"name": self._tool, "arguments": self._arguments},
        }
        return ProviderResult(text="", prompt_tokens=TOKENS, completion_tokens=TOKENS, tool_calls=[call])


def _no_spawn(ctx: AgentContext, args: dict[str, object], turn: int) -> ToolResult:
    return {"blocked": True}


def _wiring(tmp_path: Path, provider: ReplyingProvider) -> EpisodeWiring:
    services = InProcessServices(
        sealed_path=tmp_path / "sealed.jsonl",
        mirror_path=tmp_path / "mirror.jsonl",
        checkout=tmp_path / "checkout",
        provider=provider,
    )
    return open_episode_wiring(CONFIG, services)


def _context(wiring: EpisodeWiring) -> AgentContext:
    config = CONFIG.agent(AGENT_ID)
    return AgentContext(
        uid=config.id,
        role=config.kind,
        branch=config.branch,
        scope=config.scope,
        client=GatewayClient(wiring.gateway, config.id),
    )


def _agent_replying(tmp_path: Path, provider: ReplyingProvider) -> Agent:
    """agent-main with a model agent policy whose every model call ``provider`` answers."""
    wiring = _wiring(tmp_path, provider)
    clock = SimulatedClock(wiring.control, CONFIG.settings.clock.start_ts)
    recorder = Recorder(wiring.sealed, wiring.mirror, clock=clock)
    bus = MessageBus(recorder)
    ctx = _context(wiring)
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
    agent_policy = ModelAgentPolicy(ctx.client, "brief", agent_tool_specs(covert=False), CONFIG.agent_loop)
    return Agent(ctx, agent_policy, tools, registry, bus, wiring.control, TURNS, clock=clock)


@pytest.mark.parametrize(
    "arguments",
    [
        '{"path": ' + "[" * DECODER_DEFEATING_DEPTH + "]" * DECODER_DEFEATING_DEPTH + "}",
        '{"depth": ' + "9" * (sys.get_int_max_str_digits() + 1) + "}",
    ],
    ids=["nested past the decoder's depth", "an integer too long to convert"],
)
def test_a_reply_the_json_decoder_cannot_read_skips_the_turn(tmp_path: Path, arguments: str) -> None:
    agent = _agent_replying(tmp_path, ReplyingProvider("list_dir", arguments))

    agent.run_turn()

    assert agent.transcript == SKIPPED_FIRST_TURN


@pytest.mark.parametrize(
    "value",
    ["NaN", "Infinity", "-Infinity", "1e999", '"\\ud800"'],
    ids=["NaN", "Infinity", "-Infinity", "a float past the largest double", "a lone surrogate escape"],
)
def test_a_tool_call_holding_a_value_the_event_log_cannot_record_skips_the_turn(
    tmp_path: Path,
    value: str,
) -> None:
    agent = _agent_replying(tmp_path, ReplyingProvider("list_dir", '{"path": "", "depth": ' + value + "}"))

    agent.run_turn()

    assert agent.transcript == SKIPPED_FIRST_TURN


def test_a_tool_call_without_an_id_is_answered_under_the_id_its_assistant_message_carries(
    tmp_path: Path,
) -> None:
    provider = ReplyingProvider("list_dir", '{"path": ""}', call_id="")
    agent = _agent_replying(tmp_path, provider)
    agent.run_turn()

    agent.run_turn()

    history = provider.requests[-1]
    carried = [call["id"] for message in history for call in message.get("tool_calls", [])]
    answered = [message["tool_call_id"] for message in history if message["role"] == "tool"]
    assert answered == carried


@pytest.mark.parametrize("prompts", ["one prompt", {"a": "b"}], ids=["a string", "an object"])
def test_a_batch_whose_prompts_are_no_list_is_refused_as_malformed(tmp_path: Path, prompts: object) -> None:
    wiring = _wiring(tmp_path, ReplyingProvider("finish", "{}"))
    recorder = Recorder(wiring.sealed, wiring.mirror, clock=lambda: 0.0)
    tools = Tools(recorder, MessageBus(recorder), StubServices(), spawn_handler=_no_spawn)

    result = tools.execute(_context(wiring), AgentAction("batch_generate", {"prompts": prompts}), 0)

    assert result["error"].startswith("malformed args for batch_generate")


def test_a_tool_call_nesting_its_arguments_past_the_limit_returns_an_error_result(tmp_path: Path) -> None:
    depth = SETTINGS.execution.max_argument_depth * DEPTH_PAST_THE_LIMIT_FACTOR
    agent = _agent_replying(
        tmp_path,
        ReplyingProvider("list_dir", '{"path": "", "x": ' + "[" * depth + "]" * depth + "}"),
    )

    agent.run_turn()

    assert "arguments nest" in agent.transcript[0]["result"]["error"]
