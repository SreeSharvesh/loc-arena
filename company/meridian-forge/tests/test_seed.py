"""A seeded pull request is a real branch: its patch committed off main, its diff the one git computes."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from meridian_forge.forge import Forge
from meridian_forge.repositories import branch_diff
from meridian_forge.seed import ForgeSeed, SeedPullRequest

ADD_A_LINE = "--- a/NOTES.md\n+++ b/NOTES.md\n@@ -1 +1,2 @@\n first\n+second\n"
PULL_REQUEST = SeedPullRequest(
    repo="notes",
    branch="docs/second-line",
    title="Add a second line",
    author="priya",
    opened_at=datetime(2026, 8, 19, 14, 2, tzinfo=UTC),
    patch=ADD_A_LINE,
)


def test_a_seeded_pull_request_is_a_branch_whose_diff_is_its_patch(
    open_forge: Callable[[ForgeSeed], Forge],
    repositories: Path,
) -> None:
    forge = open_forge(ForgeSeed(pull_requests=(PULL_REQUEST,)))

    status = forge.pr_status({"repo": "notes", "number": 1})
    diff = branch_diff(repositories, "notes", "docs/second-line")

    assert (status["title"], status["author"]) == ("Add a second line", "priya")
    assert diff.endswith("@@ -1 +1,2 @@\n first\n+second\n")


def test_a_seed_with_a_pull_request_on_a_repo_the_forge_does_not_host_is_refused(
    open_forge: Callable[[ForgeSeed], Forge],
) -> None:
    elsewhere = PULL_REQUEST.model_copy(update={"repo": "elsewhere"})

    with pytest.raises(ValueError, match="elsewhere"):
        open_forge(ForgeSeed(pull_requests=(elsewhere,)))
