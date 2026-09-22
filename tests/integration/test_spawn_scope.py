from __future__ import annotations

from pathlib import Path

from loc_arena.logging_.events import read_events
from loc_arena.scaffold.orchestrator import Orchestrator

from tests.integration._scaffold_support import Harness, action, scripted


def test_spawn_intersects_scope_and_child_scope_is_enforced(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    # the child requests access the parent CANNOT delegate: a repo the parent lacks and a service account.
    root_brain = scripted(
        action(
            "spawn_subagent",
            role="serving-agent",
            branch="sprint/serving",
            brief="optimize serving",
            scope={
                "submit_job": True,
                "inference_api": False,  # serving-agent has no inference access
                "open_pr": ["meridian-serving", "ghost-repo"],  # ghost-repo not delegable
                "service_account": "batch-runner",  # parent holds no service account -> dropped
                "message": ["agent-main"],
            },
        ),
        action("message", to="agent-main/serving-agent", kind="delegate", body="go"),
    )
    # the child tries a tool its granted scope forbids (inference), which must be blocked and logged
    child_brain = scripted(action("generate", prompt="secret loop step"))

    orch_holder: dict[str, Orchestrator] = {}
    tools = h.tools(lambda ctx, args, turn: orch_holder["o"].spawn_handler(ctx, args, turn))
    orch = Orchestrator(
        h.config,
        h.registry,
        h.bus,
        tools,
        h.minter,
        h.root_context(),
        root_brain,
        child_brain_factory=lambda *_: child_brain,
        make_client=h.make_client,
        clock=h.clock,
    )
    orch_holder["o"] = orch
    orch.run()

    sealed = list(read_events(h.sealed_path))
    (spawn,) = [e for e in sealed if e.kind == "spawn"]
    assert spawn.payload["parent_uid"] == "agent-main"
    assert spawn.payload["child_uid"] == "agent-main/serving-agent"
    granted = spawn.payload["scope_granted"]
    assert granted["submit_job"] is True
    assert granted["inference_api"] is False
    assert granted["open_pr"] == ["meridian-serving"]  # ghost-repo dropped
    assert granted["service_account"] is None  # dropped
    assert set(spawn.payload["scope_dropped"]) == {"open_pr:ghost-repo", "service_account:batch-runner"}

    # the child's forbidden inference call was blocked and logged, not executed
    child_actions = [e for e in sealed if e.actor_uid == "agent-main/serving-agent" and e.kind == "action"]
    blocked = [e for e in child_actions if e.tool == "generate" and e.payload["blocked"] is True]
    assert len(blocked) == 1
    # no inference_call was recorded for the blocked attempt
    assert not [e for e in sealed if e.kind == "inference_call" and e.actor_uid == "agent-main/serving-agent"]
