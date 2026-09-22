"""The seeded world is a pure function of ``(config, seed)`` (reproducibility from config plus seed).

It is byte-identical across an attack run and its honest twin (the mode is still the only difference) and
reproducible from ``(config, seed)``.
"""

from __future__ import annotations

from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.forge import generate_world
from loc_arena.harness import apply_mode
from loc_arena.task import assemble_scripted_episode
from scenarios.loader import load_scenario

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
SC = load_scenario("aurora_efficiency")


def test_world_is_reproducible_from_config_and_seed() -> None:
    a = generate_world(CFG, SC, CFG.seed)
    b = generate_world(CFG, SC, CFG.seed)
    assert a.repos == b.repos
    assert a.tickets == b.tickets
    assert a.wiki == b.wiki
    assert a.ledger == b.ledger


def test_world_is_identical_across_the_attack_and_honest_twin() -> None:
    attack = apply_mode(CFG, "attack")
    honest = apply_mode(CFG, "honest")
    # covert.enabled is the only difference; the world generator never reads it
    assert attack.covert.enabled != honest.covert.enabled
    wa = generate_world(attack, SC, attack.seed)
    wh = generate_world(honest, SC, honest.seed)
    assert wa == wh


def test_episode_worlds_match_across_modes(tmp_path: Path) -> None:
    attack = assemble_scripted_episode(apply_mode(CFG, "attack"), tmp_path / "a", robust=True)
    honest = assemble_scripted_episode(apply_mode(CFG, "honest"), tmp_path / "h")
    assert attack.world == honest.world


def test_a_different_seed_gives_a_different_world() -> None:
    a = generate_world(CFG, SC, CFG.seed)
    b = generate_world(CFG, SC, CFG.seed + 1)
    assert a.repos != b.repos or a.tickets != b.tickets  # the seed actually drives the world
