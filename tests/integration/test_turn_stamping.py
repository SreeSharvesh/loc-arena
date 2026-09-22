from __future__ import annotations

from pathlib import Path

from loc_arena.logging_.events import read_events
from loc_arena.scaffold.agent import Agent
from loc_arena.scaffold.tools import AgentContext

from tests.integration._scaffold_support import Harness, action, scripted


def test_in_turn_call_stamped_spawned_code_call_off_path(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    root = h.config.agent("agent-main")  # has inference_api
    ctx = AgentContext(
        uid=root.id, role=root.kind, branch=root.branch, scope=root.scope, client=h.make_client(root.id)
    )
    tools = h.tools(lambda c, a, t: {})

    # the agent's own model call, made inside its sanctioned turn, carries a turn_id
    agent = Agent(
        ctx,
        scripted(action("generate", prompt="in-turn call", role="untrusted_agent")),
        tools,
        h.registry,
        h.bus,
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
