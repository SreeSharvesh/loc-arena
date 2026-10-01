"""The model is offered the shell, the native ``bash`` tool, only where agent code runs in its own sandbox.

Over the stack wiring each agent's code tools run in its own sandbox: the policy is offered ``bash``, and a
model's call to it runs there. In process an agent's code would run on this machine: the assembler offers no
shell, and the policy refuses a ``bash`` call as an unknown tool.
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass

import pytest
from loc_arena.config import load_run_config
from loc_arena.gateway.core import Message, ProviderResult, ToolSpec
from loc_arena.gateway.wiring import open_episode_wiring
from loc_arena.harness import apply_mode
from loc_arena.live import assemble_model_episode, run_model_policy
from loc_arena.scaffold.clock import SimulatedClock

from tests.unit._stack_services import serve_stack

CONFIG = dataclasses.replace(
    apply_mode(load_run_config("configs/aurora-efficiency.deterministic.yaml"), "honest"),
    policy="model",
)
AGENT_IDS = [agent.id for agent in CONFIG.agents]
SHELL_CALLER = "agent-main"
COMMAND = "echo from-the-sandbox"
# What the model reads back once COMMAND has run in a shell.
COMMAND_RESULT = {"exit_code": 0, "output": "from-the-sandbox\n", "truncated": False, "timed_out": False}


@dataclass(frozen=True)
class AgentCall:
    """One agent's model call as the provider got it: the tools it offered, the tool results it carried."""

    agent: str
    offered: list[str]
    tool_results: list[str]


class ShellCallingProvider:
    """A canned model: the shell caller's first reply calls ``bash``, every other agent reply ``finish``.

    It keeps every agent call it answers. A call that offers no tools (a coworker's batch) gets plain text.
    """

    def __init__(self) -> None:
        """Start with no call answered."""
        self.calls: list[AgentCall] = []

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        if not tools:
            return ProviderResult(text="ok", prompt_tokens=1, completion_tokens=1)
        named = re.match(r"You are (\S+)\.", str(messages[0]["content"]))
        call = AgentCall(
            agent=named.group(1) if named else "",
            offered=[spec["function"]["name"] for spec in tools],
            tool_results=[str(message["content"]) for message in messages if message["role"] == "tool"],
        )
        self.calls.append(call)
        name, arguments = (
            ("bash", {"command": COMMAND})
            if call.agent == SHELL_CALLER and not call.tool_results
            else ("finish", {})
        )
        tool_call = {
            "id": f"call-{len(self.calls)}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }
        return ProviderResult(text="", prompt_tokens=1, completion_tokens=1, tool_calls=[tool_call])


def _offered_bash(provider: ShellCallingProvider) -> dict[str, bool]:
    """Per agent, whether its model calls were offered ``bash``."""
    return {call.agent: "bash" in call.offered for call in provider.calls}


def _shell_callers_last_call(provider: ShellCallingProvider) -> AgentCall:
    """The shell caller's last model call: its history answers the ``bash`` call it made before."""
    return [call for call in provider.calls if call.agent == SHELL_CALLER][-1]


@pytest.fixture(scope="module")
def in_the_stack(tmp_path_factory: pytest.TempPathFactory) -> ShellCallingProvider:
    provider = ShellCallingProvider()
    served = serve_stack(tmp_path_factory.mktemp("stack"), CONFIG, provider=provider)
    wiring = open_episode_wiring(CONFIG, served.services)
    run_model_policy(
        CONFIG,
        wiring,
        SimulatedClock(wiring.control, CONFIG.settings.clock.start_ts),
        None,
        deadline_ts=CONFIG.settings.clock.deadline_ts,
        shell_offered=True,
    )
    return provider


@pytest.fixture(scope="module")
def in_process(tmp_path_factory: pytest.TempPathFactory) -> ShellCallingProvider:
    provider = ShellCallingProvider()
    assemble_model_episode(CONFIG, tmp_path_factory.mktemp("in-process"), provider=provider)
    return provider


def test_over_the_stack_wiring_every_agent_is_offered_bash(in_the_stack: ShellCallingProvider) -> None:
    offered = _offered_bash(in_the_stack)

    assert offered == dict.fromkeys(AGENT_IDS, True)


def test_over_the_stack_wiring_a_bash_call_runs_in_a_sandbox(in_the_stack: ShellCallingProvider) -> None:
    results = _shell_callers_last_call(in_the_stack).tool_results

    assert results == [json.dumps(COMMAND_RESULT)]


def test_in_process_no_agent_is_offered_bash(in_process: ShellCallingProvider) -> None:
    offered = _offered_bash(in_process)

    assert offered == dict.fromkeys(AGENT_IDS, False)


def test_in_process_a_bash_call_is_refused_as_an_unknown_tool(in_process: ShellCallingProvider) -> None:
    results = _shell_callers_last_call(in_process).tool_results

    assert results == ["unknown tool 'bash'"]
