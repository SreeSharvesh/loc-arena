from __future__ import annotations

import dataclasses

from loc_arena.config import load_run_config
from loc_arena.harness import apply_mode

_ATTACK = load_run_config("configs/aurora-efficiency.yaml")


def test_the_honest_baseline_differs_from_the_attack_in_covert_enabled_alone() -> None:
    honest = apply_mode(_ATTACK, "honest")

    assert (_ATTACK.covert.enabled, honest.covert.enabled) == (True, False)
    assert dataclasses.replace(honest, covert=dataclasses.replace(honest.covert, enabled=True)) == _ATTACK
