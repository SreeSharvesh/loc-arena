"""Scoped agent tool set.

Enforces mandatory inference routing (inference tools go only through the gateway client), the
PermissionScope on every call (via ``enforce``), and fingerprinted, append-only logging (every tool call
is a fingerprinted event on the
sealed log; a call outside scope is logged as a blocked action and NOT executed). Owns the full catalog
from the tool catalog and ``env.default.yaml`` ``tools:``. Service-backed tools
(tests, benchmark, git, tickets, wiki, cluster, ...) run behind the ``Services`` interface, stubbed here
and wired to the real services later; inference goes through the gateway client, messaging through the
bus, and spawning through the registry via an injected handler.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from loc_arena.gateway.client import GatewayClient
from loc_arena.ids import Action, PermissionScope, ScopeViolation, enforce
from loc_arena.scaffold.bus import MessageBus, Recorder
from loc_arena.scaffold.registry import SpawnDenied

# Tools whose target (repo / recipient) the scope check needs from the args.
_TARGET_FROM = {"open_pr": "repo", "merge": "repo", "message": "to", "read_weights": "name"}


@dataclass(frozen=True)
class AgentAction:
    """A structured action the scaffold parses before executing: a tool name plus its arguments."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)


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
        bus: MessageBus,
        services: Services,
        *,
        spawn_handler: SpawnHandler,
    ) -> None:
        """Wire the tool layer to the recorder, the bus, the services, and the spawn handler."""
        self._recorder = recorder
        self._bus = bus
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

        if action.tool == "message":
            # the message IS a dual-captured event via the bus; no separate action event
            event = self._bus.send(
                from_id=ctx.uid,
                from_role=ctx.role,
                to_id=str(action.args["to"]),
                kind=str(action.args.get("kind", "inform")),
                body=str(action.args.get("body", "")),
                turn=turn,
            )
            return {"message_seq": event.seq, "to": event.target_id}

        # A live model routinely emits a malformed tool call (missing/badly-typed args). That must return a
        # logged error result, NEVER crash the run -- so guard the whole dispatch against bad args.
        try:
            if action.tool == "generate":
                text = ctx.client.generate(
                    str(action.args["prompt"]), role=str(action.args.get("role", "teacher"))
                )
                result = {"text": text}
            elif action.tool == "batch_generate":
                texts = ctx.client.batch_generate(
                    [str(p) for p in action.args["prompts"]], role=str(action.args.get("role", "teacher"))
                )
                result = {"texts": texts}
            else:
                result = self._services.run(action.tool, action.args)
        except (KeyError, TypeError, ValueError) as exc:
            result = {"error": f"malformed args for {action.tool}: {exc}", "tool": action.tool}

        self._log_action(ctx, action, turn, blocked=False, result=result)
        return result
