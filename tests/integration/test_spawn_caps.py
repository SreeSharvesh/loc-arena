from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.ids import PermissionScope
from loc_arena.logging_.events import read_events
from loc_arena.scaffold.orchestrator import Orchestrator
from loc_arena.scaffold.registry import SpawnDenied

from tests.integration._scaffold_support import Harness, action, scripted

DELEGABLE = PermissionScope(spawn_subagent=True, submit_job=True)


def _spawn(
    h: Harness, parent_uid: str, parent_scope: PermissionScope, label: str
) -> tuple[str, PermissionScope]:
    return h.registry.spawn(
        parent_uid,
        parent_scope,
        label=label,
        role="serving-agent",
        branch="b",
        requested_scope=PermissionScope(submit_job=True),
        brief="x",
    )


def test_caps_yield_depth1_star(tmp_path: Path) -> None:
    h = Harness(tmp_path)  # caps: depth 1, live 6, total 7 (one sub-agent per company repo)
    root = h.config.agent("agent-main")
    for i in range(6):
        _spawn(h, root.id, root.scope, f"sub{i}")
    assert h.registry.live_subagent_count() == 6
    assert h.registry.total_created() == 7
    # a 7th spawn exceeds max_live_subagents (6) and max_total_agents (7)
    with pytest.raises(SpawnDenied):
        _spawn(h, root.id, root.scope, "sub6")


def test_depth_cap_blocks_grandchild(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    root = h.config.agent("agent-main")
    # grant the child spawn_subagent so we exercise the DEPTH cap, not the missing-capability path
    child_uid, _ = h.registry.spawn(
        root.id,
        root.scope,
        label="serving-agent",
        role="serving-agent",
        branch="b",
        requested_scope=DELEGABLE,
        brief="x",
    )
    with pytest.raises(SpawnDenied, match="depth"):
        h.registry.spawn(
            child_uid,
            DELEGABLE,
            label="grandchild",
            role="r",
            branch="b",
            requested_scope=PermissionScope(submit_job=True),
            brief="x",
        )


def test_spawn_denied_when_parent_lacks_capability(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    root = h.config.agent("agent-main")
    no_spawn = PermissionScope(spawn_subagent=False)
    with pytest.raises(SpawnDenied, match="spawn_subagent"):
        h.registry.spawn(
            root.id,
            no_spawn,
            label="x",
            role="r",
            branch="b",
            requested_scope=PermissionScope(),
            brief="x",
        )


def test_tool_boundary_logs_blocked_spawn(tmp_path: Path) -> None:
    # an agent whose scope lacks spawn_subagent tries to spawn -> blocked at the tool boundary, no child
    h = Harness(tmp_path)
    serving = h.config.agent("serving-agent")  # spawn_subagent: false
    # register serving as a live child first so it can act
    root = h.config.agent("agent-main")
    h.registry.spawn(
        root.id,
        root.scope,
        label="serving-agent",
        role="serving-agent",
        branch="b",
        requested_scope=serving.scope,
        brief="x",
    )
    orch_holder: dict[str, Orchestrator] = {}
    tools = h.tools(lambda ctx, args, turn: orch_holder["o"].spawn_handler(ctx, args, turn))
    from loc_arena.scaffold.agent import Agent
    from loc_arena.scaffold.tools import AgentContext

    ctx = AgentContext(
        uid="agent-main/serving-agent",
        role="serving-agent",
        branch="b",
        scope=serving.scope,
        client=h.make_client("agent-main/serving-agent"),
    )
    brain = scripted(action("spawn_subagent", role="r", branch="b", brief="x", scope={}))
    orch = Orchestrator(
        h.config,
        h.registry,
        h.bus,
        tools,
        h.minter,
        h.root_context(),
        scripted(),
        child_brain_factory=lambda *_: scripted(),
        make_client=h.make_client,
        clock=h.clock,
    )
    orch_holder["o"] = orch
    agent = Agent(ctx, brain, tools, h.registry, h.bus, h.minter, 5, clock=h.clock)
    agent.run()

    sealed = list(read_events(h.sealed_path))
    blocked = [
        e
        for e in sealed
        if e.kind == "action" and e.tool == "spawn_subagent" and e.payload["blocked"] is True
    ]
    assert len(blocked) == 1
    # the blocked spawn created no grandchild
    assert not [e for e in sealed if e.kind == "spawn" and e.payload["child_uid"].count("/") == 2]
