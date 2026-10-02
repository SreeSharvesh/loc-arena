"""A model reply the scaffold cannot act on costs the agent that turn only."""

from __future__ import annotations

import copy
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.gateway.core import Message, ProviderResult, ToolSpec
from loc_arena.scaffold.agent import Agent, TurnStatus
from loc_arena.scaffold.bus import MessageBus, Recorder
from loc_arena.scaffold.tools import AgentAction, StubServices, Tools

from tests.unit._stack_services import agent_context, model_agent, no_spawn, open_in_process

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
SETTINGS = CONFIG.settings
AGENT_ID = "agent-main"
TURNS = 3
TOKENS = 1
CALL_ID = "call-1"
SKIPPED_FIRST_TURN = [{"turn": 0, "skipped": True}]
# Far past the depth CPython's JSON decoder follows before it raises RecursionError.
DECODER_DEFEATING_DEPTH = 100_000
# Well past the limit, and past the ~490 levels where recording an action would raise RecursionError.
DEPTH_PAST_THE_LIMIT_FACTOR = 20


class ReplyingProvider:
    """The provider boundary, answering every call with one native tool call."""

    def __init__(
        self,
        tool: str,
        arguments: str,
        *,
        call_id: str = CALL_ID,
        more_calls: Sequence[dict[str, object]] = (),
    ) -> None:
        """Answer every call with a call of ``tool`` under ``call_id``, then the calls in ``more_calls``."""
        self._tool = tool
        self._arguments = arguments
        self._call_id = call_id
        self._more_calls = more_calls
        self.requests: list[list[Message]] = []

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        self.requests.append(copy.deepcopy(messages))
        call = {
            "id": self._call_id,
            "type": "function",
            "function": {"name": self._tool, "arguments": self._arguments},
        }
        calls = [call, *copy.deepcopy(self._more_calls)]
        return ProviderResult(text="", prompt_tokens=TOKENS, completion_tokens=TOKENS, tool_calls=calls)


def _carried_ids(history: list[Message]) -> list[str]:
    return [call["id"] for message in history for call in message.get("tool_calls", [])]


def _agent_replying(tmp_path: Path, provider: ReplyingProvider) -> Agent:
    return model_agent(CONFIG, open_in_process(tmp_path, CONFIG, provider=provider), TURNS)


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


def test_a_finish_whose_arguments_hold_a_value_the_event_log_cannot_record_ends_the_agent(
    tmp_path: Path,
) -> None:
    agent = _agent_replying(tmp_path, ReplyingProvider("finish", '{"x": NaN}'))

    status = agent.run_turn()

    assert status is TurnStatus.ENDED


def test_a_tool_call_without_an_id_is_answered_under_the_id_its_assistant_message_carries(
    tmp_path: Path,
) -> None:
    provider = ReplyingProvider("list_dir", '{"path": ""}', call_id="")
    agent = _agent_replying(tmp_path, provider)
    agent.run_turn()

    agent.run_turn()

    history = provider.requests[-1]
    answered = [message["tool_call_id"] for message in history if message["role"] == "tool"]
    assert answered == _carried_ids(history)


def test_a_tool_call_without_an_id_gets_an_id_no_other_call_of_its_reply_carries(tmp_path: Path) -> None:
    call_under_missing_0: dict[str, object] = {
        "id": "missing-0",
        "type": "function",
        "function": {"name": "list_dir", "arguments": '{"path": ""}'},
    }
    provider = ReplyingProvider("list_dir", '{"path": ""}', call_id="", more_calls=[call_under_missing_0])
    agent = _agent_replying(tmp_path, provider)
    agent.run_turn()

    agent.run_turn()

    carried = _carried_ids(provider.requests[-1])
    assert len(set(carried)) == len(carried)


def test_a_tool_call_without_an_id_gets_an_id_no_call_of_an_earlier_reply_carries(tmp_path: Path) -> None:
    provider = ReplyingProvider("list_dir", '{"path": ""}', call_id="")
    agent = _agent_replying(tmp_path, provider)
    agent.run_turn()
    agent.run_turn()

    agent.run_turn()

    carried = _carried_ids(provider.requests[-1])
    assert len(set(carried)) == len(carried)


@pytest.mark.parametrize("prompts", ["one prompt", {"a": "b"}], ids=["a string", "an object"])
def test_a_batch_whose_prompts_are_no_list_is_refused_as_malformed(tmp_path: Path, prompts: object) -> None:
    wiring = open_in_process(tmp_path, CONFIG, provider=ReplyingProvider("finish", "{}"))
    recorder = Recorder(wiring.sealed, wiring.mirror, clock=lambda: 0.0)
    tools = Tools(recorder, MessageBus(recorder), StubServices(), spawn_handler=no_spawn)
    ctx = agent_context(CONFIG, wiring, AGENT_ID)

    result = tools.execute(ctx, AgentAction("batch_generate", {"prompts": prompts}), 0)

    assert result["error"].startswith("malformed args for batch_generate")


def test_a_tool_call_nesting_its_arguments_past_the_limit_returns_an_error_result(tmp_path: Path) -> None:
    depth = SETTINGS.execution.max_argument_depth * DEPTH_PAST_THE_LIMIT_FACTOR
    agent = _agent_replying(
        tmp_path,
        ReplyingProvider("list_dir", '{"path": "", "x": ' + "[" * depth + "]" * depth + "}"),
    )

    agent.run_turn()

    assert "arguments nest" in agent.transcript[0]["result"]["error"]
