"""The episode image is built from the repository minus the `.dockerignore` patterns."""

from __future__ import annotations

import subprocess
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

REPOSITORY = Path(__file__).resolve().parents[2]


def _is_ignored(path: PurePosixPath, patterns: list[str]) -> bool:
    """Whether the path or one of its parent directories matches a pattern."""
    return any(fnmatch(str(parent), pattern) for parent in [path, *path.parents] for pattern in patterns)


def test_the_image_holds_no_scenario_subfolder_that_could_give_the_answer_away() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "scenarios"],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    patterns = (REPOSITORY / ".dockerignore").read_text().split()

    in_image = [PurePosixPath(path) for path in tracked if not _is_ignored(PurePosixPath(path), patterns)]

    assert sorted({path.parts[2] for path in in_image if len(path.parts) > 3}) == []
