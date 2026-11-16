"""A run names the engine's commit it played on, and whether its checkout differed from it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from loc_arena.run_identity import RunIdentity, read_identity


@pytest.mark.parametrize(
    ("config", "stray", "modified"),
    [
        ("configs/aurora-teams.yaml", None, False),
        ("configs/aurora-teams.yaml", "notes.txt", True),
        ("configs/aurora-teams.local.yaml", None, True),
    ],
    ids=["clean", "with a file of its own", "with a config of its own"],
)
def test_a_run_names_its_commit_and_whether_the_checkout_differs_from_it(
    tmp_path: Path,
    config: str,
    stray: str | None,
    modified: bool,
) -> None:
    git = ["git", "-C", str(tmp_path), "-c", "user.name=engine", "-c", "user.email=engine@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "aurora-teams.yaml").write_text("{}\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "engine"], check=True)
    head = subprocess.run(
        [*git, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (tmp_path / "configs" / "aurora-teams.local.yaml").write_text("{}\n")
    (tmp_path / ".git" / "info" / "exclude").write_text("*.local.yaml\n")  # a local config, as git ignores it
    if stray:
        (tmp_path / stray).write_text("draft\n")

    identity = read_identity(config, tmp_path)

    assert identity == RunIdentity(commit=head, modified=modified)


@pytest.mark.parametrize("git_found", [True, False], ids=["outside a checkout", "with no git to run"])
def test_a_run_whose_commit_git_cannot_read_records_it_unknown_and_modified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_found: bool,
) -> None:
    if not git_found:
        monkeypatch.setenv("PATH", str(tmp_path))

    identity = read_identity("aurora-efficiency", tmp_path)

    assert identity == RunIdentity(commit="unknown", modified=True)
