from __future__ import annotations

import dataclasses
from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.harness import apply_mode
from loc_arena.live import assemble_model_episode
from loc_arena.logging_.events import read_events

from tests.integration._live_support import LANDING_QUEUES, QueuedProvider

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
DEADLINE = 10_000.0


def test_live_episode_attributes_every_sealed_event_inside_the_boundary(tmp_path: Path) -> None:
    config = dataclasses.replace(apply_mode(CFG, "attack"), agent_transcript=True)
    ep = assemble_model_episode(
        config, tmp_path / "ep", provider=QueuedProvider(LANDING_QUEUES), deadline_ts=DEADLINE
    )
    assert ep.trace is not None
    sealed = list(read_events(ep.sealed_path))
    inside = [e.seq for e in sealed if e.seq <= ep.trace.last_sealed_seq]
    assert inside and all(seq in ep.trace.sealed_lane for seq in inside)
    lanes = {ref.agent_uid for ref in ep.trace.sealed_lane.values() if ref is not None}
    assert "agent-main" in lanes and len(lanes) > 1
    assert ep.trace.model_calls
    assert ep.verdict.landing.robust is True


def test_live_episode_without_the_flag_has_no_trace(tmp_path: Path) -> None:
    untraced = dataclasses.replace(apply_mode(CFG, "honest"), agent_transcript=False)
    ep = assemble_model_episode(untraced, tmp_path / "h", provider=QueuedProvider({}), deadline_ts=DEADLINE)
    assert ep.trace is None


def test_tracing_does_not_change_a_byte_of_either_log(tmp_path: Path) -> None:
    attack = dataclasses.replace(apply_mode(CFG, "attack"), agent_transcript=False)
    plain = assemble_model_episode(
        attack, tmp_path / "plain", provider=QueuedProvider(LANDING_QUEUES), deadline_ts=DEADLINE
    )
    traced = assemble_model_episode(
        dataclasses.replace(attack, agent_transcript=True),
        tmp_path / "traced",
        provider=QueuedProvider(LANDING_QUEUES),
        deadline_ts=DEADLINE,
    )
    assert traced.sealed_path.read_bytes() == plain.sealed_path.read_bytes()
    assert traced.mirror_path.read_bytes() == plain.mirror_path.read_bytes()
