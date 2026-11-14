"""The changelog check holds each release to the format and to main's merged pull requests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

CHECK = Path(__file__).resolve().parents[2] / "scripts" / "check_changelog.py"
VALID = """# Changelog

## [4] - 2026-01-03: Agents deploy the forge
### Added
- Agents can deploy merged code to the forge with `deploy` ([docs](docs/forge.md)).

## [3] - 2026-01-02: The forge keeps its pull requests
### Fixed
- The forge keeps its pull requests across a restart.

## [2] - 2026-01-02: Monitors read the mirror log
### Changed
- Monitors read the mirror log alone.

## [1] - 2026-01-01: The forge hosts git
### Added
- The forge hosts a git repository per company repo.
"""
LONG = "- The forge " + "hosts " * 29 + "git."


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A main with pull requests 1 to 3 merged, the second by a merge commit."""
    for subject in (
        "init",
        "The forge hosts git (#1)",
        "Merge pull request #2 from lab/mirror",
        "Keep them (#3)",
    ):
        subprocess.check_call(  # noqa: S603
            [
                "git",
                "-c",
                "user.name=CI",
                "-c",
                "user.email=ci@example.invalid",
                "-c",
                "init.defaultBranch=main",
            ]  # noqa: S607
            + (
                ["init", "--quiet"]
                if subject == "init"
                else ["commit", "--allow-empty", "--quiet", "-m", subject]
            ),
            cwd=tmp_path,
        )
    return tmp_path


def check(repository: Path, text: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    (repository / "CHANGELOG.md").write_text(text, encoding="utf-8")
    return subprocess.run(  # noqa: S603
        [sys.executable, str(CHECK), "--main", "main", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )


def test_check_passes_a_valid_file_whose_top_release_is_not_merged_yet(repository: Path) -> None:
    result = check(repository, VALID)

    assert (result.returncode, result.stdout) == (0, "Changelog checks passed (4 releases).\n")


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("## [3] - 2026-01-02: The", "## 3 (2026-01-02) The", "line 7: write the release heading as"),
        (
            "01-02: The forge keeps its pull requests",
            "01-32: The forge keeps",
            "line 7: 2026-01-32 is no date",
        ),
        ("hosts git\n### Added", "hosts git.\n### Added", "line 15: end the title without a full stop."),
        (
            "[3] - 2026-01-02",
            "[3] - 2026-01-04",
            "line 3: release 4 (2026-01-03) is above the later release 3",
        ),
        (
            "### Fixed",
            "### Added\n- The forge hosts git.\n### Added",
            "line 10: `Added` is twice in release 3",
        ),
        ("### Fixed", "### Deprecated", "line 8: `Deprecated` is not a type; use one of Added, Changed"),
        ("- Monitors read the mirror log alone.", LONG, "line 13: the line has 32 words; cut it to 30"),
        ("with `deploy`", "with `deploy` or `release`", "line 5: the line holds 2 code spans"),
        ("log alone.", "log alone", "line 13: end the line with a full stop."),
        ("- Monitors read", "- Now monitors read", "line 13: open the line with the part of the system"),
        ("log alone.", "log alone. They see no other.", "line 13: the line holds more than one sentence"),
        ("## [2] -", "## [9] -", "line 11: 9 is not a pull request merged into main"),
        (
            "## [2] -",
            "## [9] -",
            "pull request 2 is merged into main and has no release; add it where its merge day falls.",
        ),
    ],
)
def test_check_fails_with_what_to_change(repository: Path, old: str, new: str, message: str) -> None:
    assert old in VALID

    result = check(repository, VALID.replace(old, new))

    assert result.returncode == 1
    assert f"CHANGELOG.md: {message}" in result.stdout


def test_check_asks_the_pull_request_under_check_for_the_top_release(repository: Path) -> None:
    result = check(repository, VALID, "--pull-request", "5")

    assert result.returncode == 1
    assert "CHANGELOG.md: pull request 5 has no release; add a release for it at the top" in result.stdout


def test_check_fails_a_pull_request_check_without_main_history(tmp_path: Path) -> None:
    result = check(tmp_path, VALID, "--pull-request", "4")

    assert result.returncode == 1
    assert "no merged pull request found from main; fetch main's whole history." in result.stdout


def test_part_prints_that_part_alone_newest_first(repository: Path) -> None:
    result = check(repository, VALID, "--part", "forge")

    assert result.stdout.splitlines() == [
        "[3] 2026-01-02 Fixed: The forge keeps its pull requests across a restart.",
        "[1] 2026-01-01 Added: The forge hosts a git repository per company repo.",
    ]
