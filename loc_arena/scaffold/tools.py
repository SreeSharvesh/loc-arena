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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from loc_arena.gateway.client import GatewayClient
from loc_arena.gateway.core import ProviderError
from loc_arena.ids import Action, PermissionScope, ScopeViolation, enforce
from loc_arena.scaffold.bus import Recorder
from loc_arena.scaffold.registry import SpawnDenied

_LOGGER = logging.getLogger(__name__)

# Tools whose target (repo / recipient) the scope check needs from the args.
_TARGET_FROM = {"open_pr": "repo", "merge": "repo", "send": "to", "read_weights": "name"}
# The board's tools: ``send`` posts a message, whose ``message`` event is built from the board's record after
# play, and ``unread`` delivers an agent its messages at the start of each turn.
SEND, UNREAD = "send", "unread"


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
    ) -> None:
        """Wire the tool layer to the recorder, the services, and the spawn handler."""
        self._recorder = recorder
        self._services = services
        self._spawn_handler = spawn_handler

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

    def _record_opened_pull_request(
        self,
        ctx: AgentContext,
        action: AgentAction,
        turn: int,
        result: ToolResult,
    ) -> None:
        """Record the PR the forge opened for an agent as a ``pr`` event, as the scripted PRs are."""
        if action.tool != "open_pr" or not result.get("opened"):
            return
        self._recorder.dual(
            actor_uid=ctx.uid,
            actor_role=ctx.role,
            kind="pr",
            target_id=result["repo"],
            turn=turn,
            payload={
                "number": result["number"],
                "target_repo": result["repo"],
                "pr_title": result["title"],
                "pr_body": str(action.args.get("body", "")),
                "diff": str(action.args.get("diff", "")),
                "ci": result["ci"],
            },
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

        if action.tool == SEND:  # its record on the board becomes the message event: no action event
            return self._services.run(SEND, {**action.args, "actor_uid": ctx.uid, "actor_role": ctx.role})

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
        except ProviderError as exc:  # a covert tool's own model calls (the rogue loop) failed
            _LOGGER.warning("a model call of %s failed: %s", action.tool, exc)
            result = {"error": f"a model call of {action.tool} failed", "tool": action.tool}

        self._log_action(ctx, action, turn, blocked=False, result=result)
        self._record_opened_pull_request(ctx, action, turn, result)
        return result

    def receive(self, ctx: AgentContext) -> list[ToolResult]:
        """The messages sent to the agent that it has not received yet, from the board, in send order."""
        result = self._services.run(UNREAD, {"actor_uid": ctx.uid, "actor_role": ctx.role})
        if "error" in result:
            _LOGGER.warning("%s received no messages: %s", ctx.uid, result["error"])
        messages = result.get("messages")
        return messages if isinstance(messages, list) else []
