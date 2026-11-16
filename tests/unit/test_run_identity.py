"""A run names the config it played and the engine's commit, since two configs can share one run slug."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from loc_arena.run_identity import RunIdentity, read_identity


def test_a_run_from_a_clean_checkout_names_its_config_and_commit(tmp_path: Path) -> None:
    git = ["git", "-C", str(tmp_path), "-c", "user.name=engine", "-c", "user.email=engine@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "engine"], check=True)
    head = subprocess.run(
        [*git, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    identity = read_identity("configs/aurora-teams.yaml", tmp_path)

    assert identity == RunIdentity(config="aurora-teams", commit=head, modified=False)


@pytest.mark.parametrize("git_found", [True, False], ids=["outside a checkout", "with no git to run"])
def test_a_run_whose_commit_git_cannot_read_records_it_unknown_and_modified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_found: bool,
) -> None:
    if not git_found:
        monkeypatch.setenv("PATH", str(tmp_path))

    identity = read_identity("aurora-efficiency", tmp_path)

    assert identity == RunIdentity(config="aurora-efficiency", commit="unknown", modified=True)
