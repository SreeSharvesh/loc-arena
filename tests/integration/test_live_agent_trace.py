from __future__ import annotations

import dataclasses
from pathlib import Path

from loc_arena.config import RunConfig, load_run_config
from loc_arena.gateway.core import ScriptedProvider
from loc_arena.harness import apply_mode
from loc_arena.live import play_model_episode
from loc_arena.logging_.events import read_events
from loc_arena.task import EpisodeResult, grade_snapshot

from tests.integration._live_support import LANDING_MOVES

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
DEADLINE = 10_000.0


def _play(config: RunConfig, workdir: Path, provider: ScriptedProvider) -> EpisodeResult:
    return grade_snapshot(
        play_model_episode(config, workdir, provider=provider, deadline_ts=DEADLINE),
        config,
    )


def test_live_episode_attributes_every_sealed_event_inside_the_boundary(tmp_path: Path) -> None:
    config = dataclasses.replace(apply_mode(CFG, "attack"), agent_transcript=True)
    ep = _play(config, tmp_path / "ep", ScriptedProvider(LANDING_MOVES))
    assert ep.trace is not None
    sealed = list(read_events(ep.sealed_path))
    inside = [e.seq for e in sealed if e.seq <= ep.trace.last_sealed_seq]
    assert inside and all(seq in ep.trace.sealed_lane for seq in inside)
    lanes = {ref.agent_uid for ref in ep.trace.sealed_lane.values() if ref is not None}
    assert "agent-main" in lanes and len(lanes) > 1
    assert ep.trace.model_calls


def test_live_episode_without_the_flag_has_no_trace(tmp_path: Path) -> None:
    untraced = dataclasses.replace(apply_mode(CFG, "honest"), agent_transcript=False)
    ep = _play(untraced, tmp_path / "h", ScriptedProvider({}))
    assert ep.trace is None


def test_tracing_does_not_change_a_byte_of_either_log(tmp_path: Path) -> None:
    attack = dataclasses.replace(apply_mode(CFG, "attack"), agent_transcript=False)
    plain = _play(attack, tmp_path / "plain", ScriptedProvider(LANDING_MOVES))
    traced = _play(
        dataclasses.replace(attack, agent_transcript=True),
        tmp_path / "traced",
        ScriptedProvider(LANDING_MOVES),
    )
    assert traced.sealed_path.read_bytes() == plain.sealed_path.read_bytes()
    assert traced.mirror_path.read_bytes() == plain.mirror_path.read_bytes()
