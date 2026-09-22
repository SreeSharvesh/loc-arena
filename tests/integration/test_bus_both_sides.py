from __future__ import annotations

from pathlib import Path

from loc_arena.logging_.events import Event, fingerprint, read_events

from tests.integration._scaffold_support import Harness


def _trajectory(events: list[Event], uid: str) -> list[Event]:
    # a per-agent trajectory: events it acted on, plus messages it received
    return [e for e in events if e.actor_uid == uid or e.target_id == uid]


def test_message_lands_in_both_trajectories_and_on_both_logs(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.bus.send("agent-main", "orchestrator", "agent-main/serving-agent", "delegate", "optimize the scheduler")

    sealed = list(read_events(h.sealed_path))
    msgs = [e for e in sealed if e.kind == "message"]
    assert len(msgs) == 1
    (msg,) = msgs
    assert msg.actor_uid == "agent-main"  # sender
    assert msg.target_id == "agent-main/serving-agent"  # recipient
    assert msg.payload["content_fp"] == fingerprint("optimize the scheduler")
    assert msg.fp  # fingerprinted

    # the one message reconstructs into BOTH trajectories
    assert msg in _trajectory(sealed, "agent-main")
    assert msg in _trajectory(sealed, "agent-main/serving-agent")

    # dual capture: it is on the mirror log too (what M_action reads)
    mirror_msgs = [e for e in read_events(h.mirror_path) if e.kind == "message"]
    assert len(mirror_msgs) == 1
    assert mirror_msgs[0].actor_uid == "agent-main" and mirror_msgs[0].target_id == "agent-main/serving-agent"


def test_delivery_is_in_order_per_recipient(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    for i in range(3):
        h.bus.send("agent-main", "orchestrator", "agent-main/serving-agent", "inform", f"m{i}")
    delivered = h.bus.deliver("agent-main/serving-agent")
    assert [e.payload["body"] for e in delivered] == ["m0", "m1", "m2"]
    assert h.bus.deliver("agent-main/serving-agent") == []  # drained
