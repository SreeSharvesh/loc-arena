from __future__ import annotations

import dataclasses
from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.harness import apply_mode
from loc_arena.logging_.events import read_events
from loc_arena.task import assemble_scripted_episode

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")


def test_scripted_episode_puts_every_event_in_the_world_lane_without_changing_the_log(tmp_path: Path) -> None:
    attack = dataclasses.replace(apply_mode(CFG, "attack"), agent_transcript=False)
    plain = assemble_scripted_episode(attack, tmp_path / "plain", robust=True)
    traced = assemble_scripted_episode(
        dataclasses.replace(attack, agent_transcript=True), tmp_path / "t", robust=True
    )
    assert traced.trace is not None and plain.trace is None
    sealed = list(read_events(traced.sealed_path))
    assert traced.trace.last_sealed_seq == sealed[-1].seq
    assert {traced.trace.sealed_lane[e.seq] for e in sealed} == {None}
    assert traced.sealed_path.read_bytes() == plain.sealed_path.read_bytes()
    assert traced.mirror_path.read_bytes() == plain.mirror_path.read_bytes()
