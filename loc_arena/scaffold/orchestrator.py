"""Main-agent orchestrator loop.

Enforces the continuous main-task scoring (it drives the continuous main task) and config over code (it is
config-driven).
Owns the untrusted main agent and the cooperative star scheduler: it runs the root's turns, and
whenever a child has a pending delegation it runs that child to completion and closes it with the right
reason. It provides the ``spawn_handler`` the tool layer calls, which does the registry bookkeeping (caps,
non-escalation, the sealed ``spawn`` event) and builds the child agent with its granted scope and brief.
The agent policies are injected so a scripted episode is deterministic.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from loc_arena.board import Board
from loc_arena.config import RunConfig
from loc_arena.gateway.client import GatewayClient
from loc_arena.ids import PermissionScope
from loc_arena.scaffold.agent import Agent, AgentPolicy, TurnMinter, TurnStatus
from loc_arena.scaffold.registry import AgentRegistry, CloseReason, SpawnDenied, _scope_to_dict
from loc_arena.scaffold.tools import AgentContext, ToolResult, Tools

# Builds a child's agent policy from its spawn: (child_uid, role, brief, granted_scope) -> AgentPolicy.
ChildAgentPolicyFactory = Callable[[str, str, str, PermissionScope], AgentPolicy]
# Builds a gateway client for a given caller identity (points it at the edge).
MakeClient = Callable[[str], GatewayClient]


class Orchestrator:
    """Runs the root agent and cooperatively schedules its spawned children (the star)."""

    def __init__(
        self,
        config: RunConfig,
        registry: AgentRegistry,
        board: Board,
        tools: Tools,
        minter: TurnMinter,
        root_ctx: AgentContext,
        root_agent_policy: AgentPolicy,
        *,
        child_agent_policy_factory: ChildAgentPolicyFactory,
        make_client: MakeClient,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Wire config, the registry/board/tools/minter, the root agent, and the child factory."""
        self._config = config
        self._registry = registry
        self._board = board
        self._tools = tools
        self._minter = minter
        self._clock = clock
        self._child_agent_policy_factory = child_agent_policy_factory
        self._make_client = make_client
        self._children: dict[str, Agent] = {}
        self.root = Agent(
            root_ctx,
            root_agent_policy,
            tools,
            registry,
            minter,
            config.episode.orchestrator_turn_budget,
            clock=clock,
        )

    def spawn_handler(self, ctx: AgentContext, args: dict[str, Any], turn: int) -> ToolResult:
        """The handler the tool layer calls for ``spawn_subagent``: register, then build the child agent.

        Raises ``SpawnDenied`` (propagated by the tool layer as a blocked action) if a cap or
        non-escalation blocks it; on success returns the child uid and granted scope.
        """
        scope_arg = args["scope"]
        if not isinstance(scope_arg, dict):
            raise SpawnDenied("spawn_subagent requires a scope mapping")
        requested = PermissionScope.from_dict(scope_arg)
        role = str(args["role"])
        branch = str(args["branch"])
        brief = str(args["brief"])
        label = str(args.get("label", role))
        child_uid, granted = self._registry.spawn(
            ctx.uid,
            ctx.scope,
            label=label,
            role=role,
            branch=branch,
            requested_scope=requested,
            brief=brief,
            turn=turn,
        )
        child_client = self._make_client(child_uid)
        child_ctx = AgentContext(uid=child_uid, role=role, branch=branch, scope=granted, client=child_client)
        agent_policy = self._child_agent_policy_factory(child_uid, role, brief, granted)
        self._children[child_uid] = Agent(
            child_ctx,
            agent_policy,
            self._tools,
            self._registry,
            self._minter,
            self._config.episode.subagent_turn_budget,
            clock=self._clock,
        )
        return {"child_uid": child_uid, "scope_granted": _scope_to_dict(granted)}

    def child(self, uid: str) -> Agent:
        """The child agent with this uid (raises ``KeyError`` if it was never spawned)."""
        return self._children[uid]

    def _drain_children(self) -> None:
        """Run every live child that has a pending delegation to completion, then close it with a reason."""
        progressed = True
        while progressed:
            progressed = False
            for cuid, child in list(self._children.items()):
                if self._registry.node(cuid).status != "live":
                    continue
                if not self._board.read(cuid, self._tools.delivered_through(cuid)):
                    continue
                status = child.run()
                progressed = True
                if status is TurnStatus.BUDGET_EXHAUSTED:
                    reason = CloseReason.BUDGET_EXHAUSTED
                else:
                    reason = CloseReason.RETURNED
                self._registry.close(cuid, reason)

    def run(self) -> None:
        """Run the root agent, cooperatively scheduling children after each root turn, until the root ends."""
        while True:
            status = self.root.run_turn()
            self._drain_children()
            if status is not TurnStatus.CONTINUE:
                break
        # any child still live at the end is force-closed AFTER grading (liveness ordering, AUDITING 8.3)
        self._registry.force_close_remaining()
