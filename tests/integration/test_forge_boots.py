"""The forge boots hosting the scenario's repo(s) with a seeded, lived-in world.

Deterministic from ``(config, seed)``: a git history, open branches, open PRs, a busy ticket backlog,
CODEOWNERS, a CI config, a sparse wiki, and a partly-consumed spend ledger are all present.
"""

from __future__ import annotations

from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.forge import generate_world, hosted_repos
from loc_arena.logging_.events import read_events
from loc_arena.task import assemble_scripted_episode
from scenarios.loader import load_scenario

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
SC = load_scenario("aurora_efficiency")


def test_world_hosts_the_scenario_repos_with_seeded_history() -> None:
    repos = hosted_repos(CFG, SC)
    names = [n for n, _ in repos]
    assert "meridian-serving" in names  # the scenario's platform repo
    assert any(control for _, control in repos)  # at least one control-plane repo (monitor-policy)

    world = generate_world(CFG, SC, CFG.seed)
    platform = world.repo("meridian-serving")
    assert len(platform.commits) >= 20  # dozens of commits over months
    assert len({c.date for c in platform.commits}) > 5  # dates spread, not all one day
    assert platform.branches  # open feature branches
    assert world.open_prs("meridian-serving")  # open PRs present
    assert platform.codeowners and platform.codeowners[0].startswith("@")  # owners as @handles
    assert "pytest" in platform.ci_config  # a CI config
    assert platform.changelog.startswith("# Changelog")


def test_world_has_a_busy_backlog_wiki_and_ledger() -> None:
    world = generate_world(CFG, SC, CFG.seed)
    assert len(world.tickets) >= 10  # a busy backlog, only some run-targeted
    assert any(t.targeted_by_run for t in world.tickets)  # the run's tickets are seeded from the overlay
    assert any(not t.targeted_by_run for t in world.tickets)  # surrounded by noise
    assert len(world.wiki) >= 3  # a sparse wiki
    assert any(p.stale for p in world.wiki)  # partly stale (docs cannot substitute for code)
    assert world.ledger  # a spend ledger
    assert all(0 < e.inference_spent < e.inference_quota for e in world.ledger)  # partly consumed


def test_episode_boots_the_forge_and_attaches_the_world(tmp_path: Path) -> None:
    ep = assemble_scripted_episode(CFG, tmp_path / "ep", robust=True)
    assert ep.world.repos  # the world is attached to the episode result
    events = list(read_events(ep.sealed_path))
    boots = [e for e in events if e.kind == "forge_boot"]
    assert boots  # a forge-boot event is on the sealed log
    assert set(boots[0].payload["repos"]) == set(ep.world.repos)
