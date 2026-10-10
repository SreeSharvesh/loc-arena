"""Single agent/sub-agent turn loop.

Enforces mandatory inference routing (the agent reaches a model only through the gateway client) and
fingerprinted logging (every action it takes is recorded). The scaffold mints a signed turn token at the
start of each turn and
sets it on the agent's client, so the agent's own in-turn model calls carry a ``turn_id`` (a sanctioned
turn); code the agent spawns runs outside a turn with no token, so its calls are off-path. Each turn:
deliver the agent's unread messages from the board, ask the agent policy for this turn's actions, then
enforce scope, execute, and record each one. Bounded by the turn budget from config. The agent policy is
scripted in tests and model-backed in live runs.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import nullcontext
from enum import Enum
from typing import Any, Protocol

from loc_arena.gateway.core import sign_turn_token
from loc_arena.logging_.agent_trace import AgentTrace
from loc_arena.scaffold.registry import AgentRegistry
from loc_arena.scaffold.tools import AgentAction, AgentContext, Tools

Transcript = list[dict[str, Any]]

# An agent policy may return SKIP to yield a turn without ending (e.g. a live model's unparseable/refused
# reply); the agent records nothing and stays alive for the next round. Distinct from ``None`` (an explicit
# finish/cap).
SKIP = AgentAction(tool="__skip__")
# An agent policy returns FAILED when its model call failed: the turn is spent, and the agent stays as it was.
FAILED = AgentAction(tool="__failed__")


class AgentPolicy(Protocol):
    """Decides the next actions given the running transcript; returns ``None`` to end the loop."""

    def next_actions(self, uid: str, turn: int, transcript: Transcript) -> list[AgentAction] | None:
        """Return this turn's actions, ``[SKIP]`` to sit the turn out, or ``None`` to end."""
        ...


class ScriptedAgentPolicy:
    """A deterministic agent policy that yields a fixed sequence of actions, then ``None`` (used in tests)."""

    def __init__(self, actions: list[AgentAction]) -> None:
        """Hold the scripted actions to replay, one per turn."""
        self._actions = list(actions)
        self._i = 0

    def next_actions(self, uid: str, turn: int, transcript: Transcript) -> list[AgentAction] | None:
        """Return the next scripted action as a one-element list, or ``None`` once the script is exhausted."""
        if self._i >= len(self._actions):
            return None
        action = self._actions[self._i]
        self._i += 1
        return [action]


class TurnMinter:
    """Mints per-turn signed tokens so a sanctioned turn's calls carry a ``turn_id``."""

    def __init__(
        self,
        secret: str,
        episode_id: str,
        *,
        ttl: float = 300.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Hold the per-episode secret, the episode id, the token TTL, and the clock."""
        self._secret = secret
        self._episode_id = episode_id
        self._ttl = ttl
        self._clock = clock

    def mint(self, agent_uid: str, turn: int) -> str:
        """Sign a turn token for ``(agent_uid, turn)`` valid for ``ttl`` seconds."""
        return sign_turn_token(self._secret, self._episode_id, agent_uid, turn, self._clock() + self._ttl)


class TurnStatus(Enum):
    """The outcome of one turn: keep going, ended naturally, or the turn budget was exhausted."""

    CONTINUE = "continue"
    ENDED = "ended"
    BUDGET_EXHAUSTED = "budget_exhausted"


class Agent:
    """One agent's turn loop over scoped tools and gateway-routed model calls."""

    def __init__(
        self,
        ctx: AgentContext,
        agent_policy: AgentPolicy,
        tools: Tools,
        registry: AgentRegistry,
        minter: TurnMinter,
        turn_budget: int,
        *,
        clock: Callable[[], float] = time.time,
        trace: AgentTrace | None = None,
    ) -> None:
        """Wire the agent to its context, agent policy, tools, registry, turn minter, and budget."""
        self.ctx = ctx
        self._agent_policy = agent_policy
        self._tools = tools
        self._registry = registry
        self._minter = minter
        self._turn_budget = turn_budget
        self._clock = clock
        self._trace = trace
        self._turn = 0
        self.transcript: Transcript = []
        self._skips = 0

    # A live agent that yields this many turns in a row (a persistent refusal / malformed replies) is done, so
    # it stops consuming its budget and the round-robin driver moves on. Reset by any real action.
    _MAX_CONSECUTIVE_SKIPS = 4

    @property
    def turn(self) -> int:
        """The next turn index this agent will run."""
        return self._turn

    def run_turn(self) -> TurnStatus:
        """Run one turn: set the token, deliver unread messages, run every action, then clear the token."""
        if self._turn >= self._turn_budget:
            return TurnStatus.BUDGET_EXHAUSTED
        turn = self._turn
        self._turn += 1
        self._registry.bump_turn(self.ctx.uid)
        self.ctx.client.set_turn_token(self._minter.mint(self.ctx.uid, turn))
        try:
            with self._trace.turn(self.ctx.uid, turn) if self._trace is not None else nullcontext():
                for message in self._tools.receive(self.ctx, turn):
                    payload = {"message_kind": message.get("kind"), "body": message.get("body")}
                    self.transcript.append({"received_from": message.get("sender"), "payload": payload})
                actions = self._agent_policy.next_actions(self.ctx.uid, turn, self.transcript)
                if actions is None:
                    return TurnStatus.ENDED
                if actions == [FAILED]:
                    return TurnStatus.CONTINUE
                if len(actions) == 1 and actions[0].tool == SKIP.tool:
                    self._skips += 1
                    self.transcript.append({"turn": turn, "skipped": True})
                    return (
                        TurnStatus.ENDED
                        if self._skips >= self._MAX_CONSECUTIVE_SKIPS
                        else TurnStatus.CONTINUE
                    )
                self._skips = 0
                finishing = any(action.tool == "finish" for action in actions)
                runnable = [action for action in actions if action.tool != "finish"]
                if runnable and self._trace is not None:
                    self._trace.mark_executing()
                for action in runnable:
                    result = self._tools.execute(self.ctx, action, turn)
                    self._registry.record_activity(self.ctx.uid, self._clock())
                    self.transcript.append(
                        {"turn": turn, "tool": action.tool, "result": result, "call_id": action.call_id},
                    )
                return TurnStatus.ENDED if finishing else TurnStatus.CONTINUE
        finally:
            self.ctx.client.set_turn_token(None)

    def run(self) -> TurnStatus:
        """Run turns until the policy ends the loop or the budget is exhausted; return the terminal status."""
        while True:
            status = self.run_turn()
            if status is not TurnStatus.CONTINUE:
                return status
