"""The forge starts from the scenario's seed, and a played episode records it booting."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.forge_service import load_seed, open_forge
from loc_arena.logging_.events import read_events
from loc_arena.merged_main import EPISODE_PHASE

from meridian_forge.repositories import branch_diff
from tests._stored_run import unpack_stored_run
from tests.integration._records_support import SEALED

CFG = load_run_config("configs/aurora-efficiency.yaml")
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


def test_a_played_episode_records_the_forge_booting_once_with_the_company_repos(tmp_path: Path) -> None:
    sealed_log = unpack_stored_run(tmp_path) / EPISODE_PHASE / SEALED

    boots = [event.payload for event in read_events(sealed_log) if event.kind == "forge_boot"]

    assert boots == [{"repos": COMPANY}]
