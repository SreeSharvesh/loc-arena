"""A run names the config it played and the engine's commit, since two configs can share one run slug."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from loc_arena.episode_stack import REPOSITORY
from loc_arena.run_identity import RUN_FILE, record_identity


def test_a_run_records_its_config_by_name_and_the_checkouts_commit(tmp_path: Path) -> None:
    head = subprocess.run(["git", "-C", str(REPOSITORY), "rev-parse", "HEAD"], capture_output=True, text=True)

    record_identity(tmp_path, "configs/aurora-teams.yaml", REPOSITORY)

    recorded = json.loads((tmp_path / RUN_FILE).read_text())
    assert (recorded["config"], recorded["commit"]) == ("aurora-teams", head.stdout.strip())


def test_a_run_outside_a_checkout_records_an_unknown_commit_as_modified(tmp_path: Path) -> None:
    record_identity(tmp_path, "aurora-efficiency", tmp_path)

    recorded = json.loads((tmp_path / RUN_FILE).read_text())
    assert (recorded["commit"], recorded["modified"]) == ("unknown", True)
