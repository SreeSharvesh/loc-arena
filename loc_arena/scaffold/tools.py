"""An agent's tool layer: the native tools act on the checkout (``CodeServices``), every other goes over MCP.

An MCP call goes to the caller's client that offers it (``McpTools``); its service enforces the caller's
scope and records it, and the post-play builder (``loc_arena.recorded_events``) makes its events from that
record. So this layer logs an ``action`` only for a native call and an MCP call that never reached a service.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple

import tenacity

from loc_arena.gateway.client import GatewayClient
from loc_arena.scaffold.bus import Recorder
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.scaffold.mcp_client import McpTools

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentAction:
    """A structured action the scaffold parses before executing: a tool name plus its arguments."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None


@dataclass
class AgentContext:
    """What a tool needs to act on behalf of an agent: its uid, role, branch and gateway client."""

    uid: str
    role: str
    branch: str
    client: GatewayClient


ToolResult = dict[str, Any]


class _Outcome(NamedTuple):
    """A call's result, and whether a service took the call, whose record then makes its events."""

    result: ToolResult
    recorded: bool


class Tools:
    """Executes an agent's action: dispatch it, and log it unless a service records it."""

    def __init__(
        self,
        recorder: Recorder,
        code: CodeServices,
        clients: Mapping[str, Sequence[McpTools]],
        *,
        delivery_attempts: int = 1,
    ) -> None:
        """Wire the tool layer to the recorder, the native tools, each agent's MCP clients by its id."""
        self._recorder = recorder
        self._delivery_attempts = delivery_attempts
        self._delivered: dict[str, int] = {}  # by agent: the id of the last message delivered to it
        self._code = code
        self._clients = clients

    def _log_action(self, ctx: AgentContext, action: AgentAction, turn: int, result: ToolResult) -> None:
        """Record a tool call as a dual (sealed + mirror) action event so M_action can read it."""
        self._recorder.dual(
            actor_uid=ctx.uid,
            actor_role=ctx.role,
            kind="action",
            tool=action.tool,
            target_id=None,
            turn=turn,
            payload={"args": action.args, "target": None, "blocked": False, "reason": None},
            result=result,
        )

    def _run(self, uid: str, tool: str, args: dict[str, Any]) -> _Outcome:
        """Run a native tool on the checkout, else ``tool`` over MCP through ``uid``'s client offering it.

        Either target answers a malformed or failed call with an error result, never an exception.
        """
        if tool in CodeServices.TOOLS:
            return _Outcome(self._code.run(tool, {**args, "actor_uid": uid}), recorded=False)
        try:
            client = next((client for client in self._clients.get(uid, ()) if tool in client.names()), None)
            if client is None:
                return _Outcome({"error": f"{tool} is not offered to you", "tool": tool}, recorded=False)
            return _Outcome(client.call(tool, args), recorded=True)
        except Exception as error:  # noqa: BLE001 - a broken connection is the agent's error result
            _LOGGER.warning("%s could not be called: %r", tool, error)
            unreachable = f"{tool} could not be called: the tool server is unreachable"
            return _Outcome({"error": unreachable, "tool": tool}, recorded=False)

    def execute(self, ctx: AgentContext, action: AgentAction, turn: int) -> ToolResult:
        """Dispatch the call, and log it as an action unless a service took it."""
        outcome = self._run(ctx.uid, action.tool, action.args)
        if not outcome.recorded:
            self._log_action(ctx, action, turn, outcome.result)
        return outcome.result

    def receive(self, ctx: AgentContext, turn: int) -> list[ToolResult]:
        """The messages sent to the agent after the last one it received, from the board, in send order.

        It reads after the last id it delivered, so an attempt whose answer was lost loses no message. A
        delivery that fails every attempt is recorded as the agent's ``read`` action, with its error.
        """
        args = {"after": self.delivered_through(ctx.uid)}
        outcome = tenacity.Retrying(
            stop=tenacity.stop_after_attempt(self._delivery_attempts),
            wait=tenacity.wait_exponential(multiplier=0.25, max=2),
            retry=tenacity.retry_if_result(lambda outcome: "error" in outcome.result),
            retry_error_callback=lambda state: (
                state.outcome.result() if state.outcome else _Outcome({}, recorded=False)
            ),
        )(self._run, ctx.uid, "read", args)
        if "error" in outcome.result and not outcome.recorded:
            self._log_action(ctx, AgentAction("read", args), turn, outcome.result)
        messages = outcome.result.get("messages", [])
        self._delivered[ctx.uid] = messages[-1]["id"] if messages else args["after"]
        return messages

    def delivered_through(self, uid: str) -> int:
        """The id of the last message delivered to ``uid``, 0 before any."""
        return self._delivered.get(uid, 0)
