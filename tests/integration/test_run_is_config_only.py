from __future__ import annotations

import dataclasses
from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.logging_.events import Event, read_events
from loc_arena.task import assemble_scripted_episode

_ATTACK = load_run_config("configs/aurora-efficiency.deterministic.yaml")
_HONEST = dataclasses.replace(_ATTACK, covert=dataclasses.replace(_ATTACK.covert, enabled=False))


def _off_path_calls(sealed_path: Path) -> list[Event]:
    return [
        e
        for e in read_events(sealed_path)
        if e.kind == "inference_call"
        and e.payload.get("turn_id") is None
        and e.payload.get("batch_id") is None
    ]


def test_covert_enabled_is_the_only_config_difference() -> None:
    # the twin config equals the attack config with covert.enabled flipped -- nothing else differs
    assert _HONEST == dataclasses.replace(_ATTACK, covert=dataclasses.replace(_ATTACK.covert, enabled=False))
    assert dataclasses.replace(_HONEST, covert=dataclasses.replace(_HONEST.covert, enabled=True)) == _ATTACK
    assert _HONEST.covert.enabled is False and _ATTACK.covert.enabled is True


def test_only_covert_toggle_changes_the_episode(tmp_path: Path) -> None:
    honest = assemble_scripted_episode(_HONEST, tmp_path / "h")
    attack = assemble_scripted_episode(_ATTACK, tmp_path / "a")
    # identical main-task work (the optimization cover is the same)
    assert honest.main_score.final_score == attack.main_score.final_score
    # the honest twin produces NO off-path inference; the attack does -- that is the whole difference
    assert _off_path_calls(honest.sealed_path) == []
    assert len(_off_path_calls(attack.sealed_path)) > 0
    assert honest.verdict.landing.minimal is False
    assert attack.verdict.landing.minimal is True
