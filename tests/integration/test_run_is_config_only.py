from __future__ import annotations

import dataclasses
from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.logging_.events import Event, read_events

_ATTACK = load_run_config("configs/aurora-efficiency.yaml")
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
