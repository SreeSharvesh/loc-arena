"""The episode image is built from the repository minus the `.dockerignore` patterns."""

from __future__ import annotations

import subprocess
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

REPOSITORY = Path(__file__).resolve().parents[2]


def _is_ignored(path: PurePosixPath, patterns: list[str]) -> bool:
    """Whether the path or a parent matches a pattern, segment by segment as Docker does."""
    prefixes = [path.parts[: length + 1] for length in range(len(path.parts))]
    return any(
        len(prefix) == len(segments) and all(map(fnmatch, prefix, segments))
        for prefix in prefixes
        for segments in (PurePosixPath(pattern).parts for pattern in patterns)
    )


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

    # Only what the episode, loading the pack, checks: each live service's build directory has a Dockerfile, and
    # the skills folder, which every sandbox mounts anyway, exists.
    assert sorted({path.parts[2] for path in in_image if len(path.parts) > 3}) == ["services", "skills"]
