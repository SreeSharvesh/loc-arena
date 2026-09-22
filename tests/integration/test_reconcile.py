from __future__ import annotations

from pathlib import Path

from loc_arena.ids import PermissionScope
from loc_arena.scaffold.registry import CloseReason

from tests.integration._scaffold_support import Harness


def _spawn_child(h: Harness, label: str) -> str:
    root = h.config.agent("agent-main")
    cuid, _ = h.registry.spawn(
        root.id,
        root.scope,
        label=label,
        role="r",
        branch="b",
        requested_scope=PermissionScope(submit_job=True),
        brief="x",
    )
    return cuid


def test_clean_episode_reconciles(tmp_path: Path) -> None:
    h = Harness(tmp_path, episode_id="ep-rec1")
    cuid = _spawn_child(h, "serving-agent")
    h.registry.close(cuid, CloseReason.RETURNED)
    report = h.registry.reconcile()
    assert report.ok is True
    assert report.live_count == 0
    assert report.unmatched_spawns == ()


def test_orphan_activity_is_surfaced(tmp_path: Path) -> None:
    h = Harness(tmp_path, episode_id="ep-rec2")
    # a synthetic action under an actor_uid that was never spawned
    h.recorder.sealed(
        actor_uid="ghost/rogue-agent",
        actor_role="untrusted",
        kind="action",
        tool="bash",
        payload={"args": {}},
    )
    report = h.registry.reconcile()
    assert report.ok is False
    assert len(report.orphan_activity) == 1


def test_post_close_activity_is_surfaced(tmp_path: Path) -> None:
    h = Harness(tmp_path, episode_id="ep-rec3")
    cuid = _spawn_child(h, "serving-agent")
    h.registry.close(cuid, CloseReason.RETURNED)
    # a synthetic event by the agent AFTER its close (the untracked-persistence signal, ties to
    # persistent_compute_job/underreported_activity_logging)
    h.recorder.sealed(
        actor_uid=cuid, actor_role="untrusted", kind="action", tool="generate", payload={"args": {}}
    )
    report = h.registry.reconcile()
    assert report.ok is False
    assert len(report.post_close_activity) == 1
