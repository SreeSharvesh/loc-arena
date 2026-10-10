from __future__ import annotations

from pathlib import Path

from loc_arena.gateway.client import GatewayClient
from loc_arena.logging_.events import read_events
from loc_arena.scaffold.agent import Agent, Transcript
from loc_arena.scaffold.tools import AgentAction, AgentContext

from tests.integration._scaffold_support import Harness


class AsksTheModelOnce:
    """An agent policy that asks the model once in its turn, as a live agent's policy does, then ends."""

    def __init__(self, client: GatewayClient) -> None:
        """Ask the model through ``client``."""
        self._client = client

    def next_actions(self, uid: str, turn: int, transcript: Transcript) -> list[AgentAction] | None:
        self._client.generate("in-turn call", role="untrusted_agent")
        return None


def test_in_turn_call_stamped_spawned_code_call_off_path(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    root = h.config.agent("agent-main")  # has inference_api
    ctx = AgentContext(
        uid=root.id,
        role=root.kind,
        branch=root.branch,
        scope=root.scope,
        client=h.make_client(root.id),
    )

    # the agent's own model call, made inside its sanctioned turn, carries a turn_id
    agent = Agent(
        ctx,
        AsksTheModelOnce(ctx.client),
        h.tools(),
        h.registry,
        h.minter,
        5,
        clock=h.clock,
    )
    agent.run_turn()

    # code the agent spawned runs OUTSIDE any turn: a raw client with no turn token -> off-path
    worker_client = h.make_client(root.id)  # no set_turn_token -> token is None
    worker_client.generate("off-turn worker call", role="untrusted_agent")

    inf = [e for e in read_events(h.sealed_path) if e.kind == "inference_call"]
    assert len(inf) == 2
    in_turn = next(e for e in inf if e.payload["turn_id"] is not None)
    off_turn = next(e for e in inf if e.payload["turn_id"] is None)
    assert in_turn.payload["turn_id"] == "agent-main:t0"
    assert in_turn.payload["credential_kind"] == "turn_token"
    assert off_turn.payload["batch_id"] is None
    assert off_turn.payload["credential_kind"] == "none"
