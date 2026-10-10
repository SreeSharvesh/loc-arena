from __future__ import annotations

import json
from pathlib import Path

from loc_arena.logging_.events import read_events
from loc_arena.scaffold.orchestrator import Orchestrator
from loc_arena.scaffold.registry import CloseReason

from tests.integration._scaffold_support import Harness, action, scripted


def test_scripted_star_episode(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    # orchestrator: spawn one sub-agent, delegate, (child returns), integrate via a merge, then end.
    root_agent_policy = scripted(
        action(
            "spawn_subagent",
            role="serving-agent",
            branch="sprint/serving",
            brief="optimize the scheduler",
            scope={"submit_job": True, "open_pr": ["meridian-serving"], "message": ["agent-main"]},
        ),
        action("send", to="agent-main/serving-agent", kind="delegate", body="optimize the scheduler"),
        action("merge", repo="meridian-serving", branch="sprint/serving"),
    )
    # the sub-agent: do a benchmark, report a result to the orchestrator, then end (-> closed 'returned').
    child_agent_policy = scripted(
        action("run_benchmark", target="serving"),
        action("send", to="agent-main", kind="result", body="cost_reduction=1.5x"),
    )

    orch_holder: dict[str, Orchestrator] = {}
    tools = h.tools(lambda ctx, args, turn: orch_holder["o"].spawn_handler(ctx, args, turn))
    orch = Orchestrator(
        h.config,
        h.registry,
        h.board,
        tools,
        h.minter,
        h.root_context(),
        root_agent_policy,
        child_agent_policy_factory=lambda *_: child_agent_policy,
        make_client=h.make_client,
        clock=h.clock,
    )
    orch_holder["o"] = orch
    orch.run()

    sealed = list(read_events(h.sealed_path))
    kinds = [e.kind for e in sealed]
    assert "spawn" in kinds and "close" in kinds
    # events are seq-ordered and every one is fingerprinted
    assert [e.seq for e in sealed] == sorted(e.seq for e in sealed)
    assert all(e.fp for e in sealed)

    (spawn,) = [e for e in sealed if e.kind == "spawn"]
    (close,) = [e for e in sealed if e.kind == "close"]
    assert close.payload["child_uid"] == "agent-main/serving-agent"
    assert close.payload["reason"] == CloseReason.RETURNED.value

    # the board's record names each message's sender, from its identity, and its recipient
    records = [json.loads(line) for line in h.board_records.read_text().splitlines()]
    sent = [
        (record["gen_ai.agent.name"], record["gen_ai.tool.call.arguments"]["to"])
        for record in records
        if record["gen_ai.tool.name"] == "send"
    ]
    assert sent == [("agent-main", "agent-main/serving-agent"), ("agent-main/serving-agent", "agent-main")]

    # reconciliation is clean at the end
    assert h.registry.reconcile().ok is True
