"""Scoped agent tool set.

Enforces mandatory inference routing (inference tools go only through the gateway client), the
PermissionScope on every call (via ``enforce``), and fingerprinted, append-only logging (every tool call
is a fingerprinted event on the
sealed log; a call outside scope is logged as a blocked action and NOT executed). Owns the full catalog
from the tool catalog and ``env.default.yaml`` ``tools:``. Service-backed tools
(tests, benchmark, git, tickets, wiki, cluster, ...) run behind the ``Services`` interface, stubbed here
and wired to the real services later; inference goes through the gateway client, messaging through the
board (an MCP service), and spawning through the registry via an injected handler.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Any, Protocol

import tenacity

from loc_arena.gateway.client import GatewayClient
from loc_arena.gateway.core import ProviderError
from loc_arena.ids import Action, PermissionScope, ScopeViolation, enforce
from loc_arena.scaffold.bus import Recorder
from loc_arena.scaffold.registry import SpawnDenied

_LOGGER = logging.getLogger(__name__)

# Tools whose target (repo / recipient) the scope check needs from the args.
_TARGET_FROM = {"open_pr": "repo", "merge": "repo", "send": "to", "read_weights": "name"}


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
# The spawn handler the orchestrator injects: (context, args, turn) -> result; raises SpawnDenied on a cap.
SpawnHandler = Callable[[AgentContext, dict[str, Any], int], ToolResult]


class Services(Protocol):
    """The service clients, invoked by name. Stubbed early, real once the services exist."""

    def run(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Run a service-backed tool and return its structured result."""
        ...


class StubServices:
    """A deterministic stand-in for the services: every call returns a canned, fingerprintable result."""

    def run(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Return a canned result for a service-backed tool."""
        return {"stub": True, "tool": tool}


class Tools:
    """Executes an agent's action: enforce scope, dispatch, and record the call (or the blocked attempt)."""

    def __init__(
        self,
        recorder: Recorder,
        services: Services,
        *,
        spawn_handler: SpawnHandler,
        delivery_attempts: int = 1,
        recorded_elsewhere: Collection[str] = (),
    ) -> None:
        """Wire the tool layer to the recorder, the services, the spawn handler and the delivery attempts.

        A tool in ``recorded_elsewhere`` is one a service records and the post-play builder rebuilds the
        event for (the covert tools), so a successful call logs no action event here, which would double it.
        """
        self._recorder = recorder
        self._delivery_attempts = delivery_attempts
        self._delivered: dict[str, int] = {}  # by agent: the id of the last message delivered to it
        self._services = services
        self._spawn_handler = spawn_handler
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

    def execute(self, ctx: AgentContext, action: AgentAction, turn: int) -> ToolResult:
        """Enforce scope, then dispatch and record the call; a scope violation is logged and not executed."""
        target = self._target_for(action)
        ids_action = Action(tool=action.tool, args={"target": target} if target is not None else {})
        try:
            enforce(ctx.scope, ids_action, actor_uid=ctx.uid)
        except ScopeViolation as exc:
            self._log_action(ctx, action, turn, blocked=True, reason=exc.reason)
            return {"blocked": True, "reason": exc.reason}

        if action.tool == "spawn_subagent":
            try:
                result = self._spawn_handler(ctx, action.args, turn)
            except SpawnDenied as exc:
                self._log_action(ctx, action, turn, blocked=True, reason=exc.reason)
                return {"blocked": True, "reason": exc.reason}
            self._log_action(ctx, action, turn, blocked=False, result=result)
            return result

        if action.tool == "send":  # the board's record of a message it takes becomes the message event
            result = self._services.run("send", {**action.args, "actor_uid": ctx.uid, "actor_role": ctx.role})
            if "error" in result:
                self._log_action(ctx, action, turn, blocked=False, result=result)
            return result

        # A live model routinely emits a malformed tool call (missing/badly-typed args). That must return a
        # logged error result, NEVER crash the run -- so guard the whole dispatch against bad args.
        try:
            if action.tool == "generate":
                text = ctx.client.generate(
                    str(action.args["prompt"]),
                    role=str(action.args.get("role", "teacher")),
                )
                result = {"text": text}
            elif action.tool == "batch_generate":
                texts = ctx.client.batch_generate(
                    [str(p) for p in action.args["prompts"]],
                    role=str(action.args.get("role", "teacher")),
                )
                result = {"texts": texts}
            else:
                # The caller goes last, so an agent cannot name another: the covert tools record it.
                result = self._services.run(
                    action.tool,
                    {**action.args, "actor_uid": ctx.uid, "actor_role": ctx.role},
                )
        except (KeyError, TypeError, ValueError) as exc:
            result = {"error": f"malformed args for {action.tool}: {exc}", "tool": action.tool}
        except ProviderError as exc:  # a tool's own model calls failed
            _LOGGER.warning("a model call of %s failed: %s", action.tool, exc)
            result = {"error": f"a model call of {action.tool} failed", "tool": action.tool}

        # A tool a service records is logged from its record after play, so logging it here would double it.
        if action.tool not in self._recorded_elsewhere:
            self._log_action(ctx, action, turn, blocked=False, result=result)
        return result

    def receive(self, ctx: AgentContext, turn: int) -> list[ToolResult]:
        """The messages sent to the agent after the last one it received, from the board, in send order.

        It reads after the last id it delivered, so an attempt whose answer was lost loses no message. A
        delivery that fails every attempt is recorded as the agent's ``read`` action, with its error.
        """
        args = {"after": self.delivered_through(ctx.uid), "actor_uid": ctx.uid, "actor_role": ctx.role}
        result = tenacity.Retrying(
            stop=tenacity.stop_after_attempt(self._delivery_attempts),
            wait=tenacity.wait_exponential(multiplier=0.25, max=2),
            retry=tenacity.retry_if_result(lambda result: "error" in result),
            retry_error_callback=lambda state: state.outcome.result() if state.outcome else {},
        )(self._services.run, "read", args)
        if "error" in result:
            delivery = AgentAction("read", {"after": args["after"]})
            self._log_action(ctx, delivery, turn, blocked=False, result=result)
        messages = result.get("messages", [])
        self._delivered[ctx.uid] = messages[-1]["id"] if messages else args["after"]
        return messages

    def delivered_through(self, uid: str) -> int:
        """The id of the last message delivered to ``uid``, 0 before any."""
        return self._delivered.get(uid, 0)
