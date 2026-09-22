from __future__ import annotations

import dataclasses
from pathlib import Path

from loc_arena.ids import PermissionScope
from loc_arena.logging_.events import read_events
from loc_arena.scaffold.registry import AgentRegistry, CloseReason

from tests.integration._scaffold_support import Harness


def _registry_with_teardown(tmp_path: Path) -> tuple[Harness, list[str]]:
    h = Harness(tmp_path, episode_id="ep-close")
    torn: list[str] = []
    root = h.config.agent("agent-main")
    # relaxed caps: this test exercises close reasons, not caps (agents created are cumulative)
    episode = dataclasses.replace(h.config.episode, max_total_agents=100, max_live_subagents=100)
    reg = AgentRegistry(
        episode,
        h.recorder,
        str(h.sealed_path),
        root_uid=root.id,
        root_role=root.kind,
        root_branch=root.branch,
        root_scope=root.scope,
        teardown=torn.append,
        clock=h.clock,
    )
    h.registry = reg
    return h, torn


def test_each_close_reason_writes_sealed_event_and_tears_down(tmp_path: Path) -> None:
    h, torn = _registry_with_teardown(tmp_path)
    root = h.config.agent("agent-main")
    reasons = [
        CloseReason.RETURNED,
        CloseReason.CLOSED_BY_PARENT,
        CloseReason.BUDGET_EXHAUSTED,
        CloseReason.ERROR,
        CloseReason.EPISODE_END,
    ]
    for i, reason in enumerate(reasons):
        cuid, _ = h.registry.spawn(
            root.id,
            PermissionScope(spawn_subagent=True),
            label=f"c{i}",
            role="r",
            branch="b",
            requested_scope=PermissionScope(),
            brief="x",
        )
        h.registry.bump_turn(cuid)
        h.registry.close(cuid, reason)
        assert cuid in torn  # sandbox torn down

    sealed = list(read_events(h.sealed_path))
    closes = {e.payload["child_uid"]: e.payload for e in sealed if e.kind == "close"}
    assert {p["reason"] for p in closes.values()} == {r.value for r in reasons}
    assert all("final_turn_count" in p for p in closes.values())


def test_idle_reaper_closes_inactive_child(tmp_path: Path) -> None:
    h, torn = _registry_with_teardown(tmp_path)
    root = h.config.agent("agent-main")
    cuid, _ = h.registry.spawn(
        root.id,
        PermissionScope(spawn_subagent=True),
        label="idler",
        role="r",
        branch="b",
        requested_scope=PermissionScope(),
        brief="x",
    )
    # advance past the idle timeout with no activity
    h.clock.advance(h.config.episode.subagent_idle_timeout_seconds + 1)
    reaped = h.registry.reap_idle()
    assert reaped == [cuid]
    sealed = list(read_events(h.sealed_path))
    (close,) = [e for e in sealed if e.kind == "close"]
    assert close.payload["reason"] == CloseReason.IDLE.value
    assert cuid in torn


def test_force_close_remaining_uses_episode_end(tmp_path: Path) -> None:
    h, _ = _registry_with_teardown(tmp_path)
    root = h.config.agent("agent-main")
    h.registry.spawn(
        root.id,
        PermissionScope(spawn_subagent=True),
        label="live1",
        role="r",
        branch="b",
        requested_scope=PermissionScope(),
        brief="x",
    )
    closed = h.registry.force_close_remaining()
    assert len(closed) == 1
    (close,) = [e for e in read_events(h.sealed_path) if e.kind == "close"]
    assert close.payload["reason"] == CloseReason.EPISODE_END.value
    assert h.registry.live_subagent_count() == 0
