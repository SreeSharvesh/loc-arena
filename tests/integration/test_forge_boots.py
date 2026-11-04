"""The forge starts from the scenario's seed, and the episode records it booting."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.forge_service import load_seed, open_forge
from loc_arena.logging_.events import read_events

from meridian_forge.repositories import branch_diff
from tests.integration._live_support import play_graded_episode

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
COMPANY = [
    "meridian-common",
    "meridian-controlplane",
    "meridian-datapipe",
    "meridian-distill",
    "meridian-evalkit",
    "meridian-jobsvc",
    "meridian-serving",
]


def test_every_pull_request_of_the_scenarios_seed_opens_on_a_branch_of_its_repo(tmp_path: Path) -> None:
    seed = load_seed(CFG)

    forge = open_forge(CFG, seed, tmp_path)

    numbers = Counter[str]()
    opened = []
    for pr in seed.pull_requests:
        numbers[pr.repo] += 1  # each takes the number after its repo's last
        status = forge.pr_status({"repo": pr.repo, "number": numbers[pr.repo]})
        opened.append((status["title"], status["author"], bool(branch_diff(tmp_path, pr.repo, pr.branch))))
    assert opened == [(pr.title, pr.author, True) for pr in seed.pull_requests]


def test_the_episode_records_the_forge_booting_with_the_company_repos(tmp_path: Path) -> None:
    episode = play_graded_episode(CFG, tmp_path / "ep")

    boots = [event.payload for event in read_events(episode.sealed_path) if event.kind == "forge_boot"]

    assert boots == [{"repos": COMPANY}]
