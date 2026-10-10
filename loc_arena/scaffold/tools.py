"""Scoped agent tool set.

Enforces the PermissionScope on every call (via ``enforce``) and fingerprinted, append-only logging (every
tool call is a fingerprinted event on the sealed log; a call outside scope is logged as a blocked action and
NOT executed). A tool runs on one of two targets: the native tools act on the checkout (``CodeServices``),
and every other tool goes over MCP to the caller's client that offers it (``McpTools``), the board included.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import tenacity

from loc_arena.gateway.client import GatewayClient
from loc_arena.ids import Action, PermissionScope, ScopeViolation, enforce
from loc_arena.scaffold.bus import Recorder
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.scaffold.mcp_client import McpTools

# Tools whose target (repo / recipient) the scope check needs from the args.
_TARGET_FROM = {"open_pr": "repo", "send": "to"}


@dataclass(frozen=True)
class AgentAction:
    """A structured action the scaffold parses before executing: a tool name plus its arguments."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None


@dataclass
class AgentContext:
    """What a tool needs to act on behalf of an agent: its uid/role/branch, scope, and gateway client."""

    uid: str
    role: str
    branch: str
    scope: PermissionScope
    client: GatewayClient


ToolResult = dict[str, Any]


class Tools:
    """Executes an agent's action: enforce scope, dispatch, and record the call (or the blocked attempt)."""

    def __init__(
        self,
        recorder: Recorder,
        code: CodeServices,
        clients: Mapping[str, Sequence[McpTools]],
        *,
        delivery_attempts: int = 1,
        recorded_elsewhere: Collection[str] = (),
    ) -> None:
        """Wire the tool layer to the recorder, the native tools, each agent's MCP clients by its id.

        A tool in ``recorded_elsewhere`` is one a service records and the post-play builder rebuilds the
        event for (the covert tools), so a successful call logs no action event here, which would double it.
        """
        self._recorder = recorder
        self._delivery_attempts = delivery_attempts
        self._delivered: dict[str, int] = {}  # by agent: the id of the last message delivered to it
        self._code = code
        self._clients = clients
        self._recorded_elsewhere = frozenset(recorded_elsewhere)

    @staticmethod
    def _target_for(action: AgentAction) -> str | None:
        key = _TARGET_FROM.get(action.tool)
        if key is None:
            return None
        val = action.args.get(key)
        return val if isinstance(val, str) else None

    def _log_action(
        self,
        ctx: AgentContext,
        action: AgentAction,
        turn: int,
        *,
        blocked: bool,
        reason: str | None = None,
        result: ToolResult | None = None,
    ) -> None:
        """Record a tool-call as a dual (sealed + mirror) action event so M_action can read it."""
        self._recorder.dual(
            actor_uid=ctx.uid,
            actor_role=ctx.role,
            kind="action",
            tool=action.tool,
            target_id=self._target_for(action),
            turn=turn,
            payload={
                "args": action.args,
                "target": self._target_for(action),
                "blocked": blocked,
                "reason": reason,
            },
            result=result,
        )

    def _run(self, uid: str, tool: str, args: dict[str, Any]) -> ToolResult:
        """Run a native tool on the checkout, else ``tool`` over MCP through ``uid``'s client offering it.

        Either target answers a malformed or failed call with an error result, never an exception.
        """
        if tool in CodeServices.TOOLS:
            return self._code.run(tool, {**args, "actor_uid": uid})
        client = next((client for client in self._clients.get(uid, ()) if tool in client.names()), None)
        return client.call(tool, args) if client else {"error": f"{tool} is not offered to you", "tool": tool}

    def execute(self, ctx: AgentContext, action: AgentAction, turn: int) -> ToolResult:
        """Enforce scope, then dispatch and record the call; a scope violation is logged and not executed."""
        target = self._target_for(action)
        ids_action = Action(tool=action.tool, args={"target": target} if target is not None else {})
        try:
            enforce(ctx.scope, ids_action, actor_uid=ctx.uid)
        except ScopeViolation as exc:
            self._log_action(ctx, action, turn, blocked=True, reason=exc.reason)
            return {"blocked": True, "reason": exc.reason}

        result = self._run(ctx.uid, action.tool, action.args)
        # The board's record of a message it takes becomes the message event, and a tool a service records is
        # logged from its record after play, so logging either here would double it.
        sent = action.tool == "send" and "error" not in result
        recorded = sent or action.tool in self._recorded_elsewhere
        if not recorded:
            self._log_action(ctx, action, turn, blocked=False, result=result)
        return result

    def receive(self, ctx: AgentContext, turn: int) -> list[ToolResult]:
        """The messages sent to the agent after the last one it received, from the board, in send order.

        It reads after the last id it delivered, so an attempt whose answer was lost loses no message. A
        delivery that fails every attempt is recorded as the agent's ``read`` action, with its error.
        """
        args = {"after": self.delivered_through(ctx.uid)}
        result = tenacity.Retrying(
            stop=tenacity.stop_after_attempt(self._delivery_attempts),
            wait=tenacity.wait_exponential(multiplier=0.25, max=2),
            retry=tenacity.retry_if_result(lambda result: "error" in result),
            retry_error_callback=lambda state: state.outcome.result() if state.outcome else {},
        )(self._run, ctx.uid, "read", args)
        if "error" in result:
            self._log_action(ctx, AgentAction("read", args), turn, blocked=False, result=result)
        messages = result.get("messages", [])
        self._delivered[ctx.uid] = messages[-1]["id"] if messages else args["after"]
        return messages

    def delivered_through(self, uid: str) -> int:
        """The id of the last message delivered to ``uid``, 0 before any."""
        return self._delivered.get(uid, 0)
