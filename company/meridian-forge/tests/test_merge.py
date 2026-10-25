"""A pull request names a pushed branch, and a merge is git's own merge of it onto main, under the gate."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from meridian_forge.forge import Forge, ForgeError
from meridian_forge.repositories import commit_branch, identity, repository_path
from meridian_forge.seed import ForgeSeed

SECOND_LINE = "--- a/NOTES.md\n+++ b/NOTES.md\n@@ -1 +1,2 @@\n first\n+second\n"
OTHER_SECOND_LINE = "--- a/NOTES.md\n+++ b/NOTES.md\n@@ -1 +1,2 @@\n first\n+another\n"


def push(repositories: Path, branch: str, patch: str) -> None:
    commit_branch(repositories, "notes", branch, patch, f"Change {branch}", identity("priya"))


def read_main(repositories: Path) -> str:
    bare = str(repository_path(repositories, "notes"))
    shown = ["git", "--git-dir", bare, "show", "main:NOTES.md"]
    return subprocess.run(shown, capture_output=True, text=True, check=True).stdout


def test_a_merge_puts_the_branch_on_main(
    open_forge: Callable[[ForgeSeed], Forge],
    repositories: Path,
) -> None:
    forge = open_forge(ForgeSeed())
    push(repositories, "docs/second", SECOND_LINE)
    opened = forge.open_pr({"repo": "notes", "branch": "docs/second", "actor_uid": "priya"})

    merged = forge.merge({"repo": "notes", "number": opened["number"], "actor_uid": "priya"})

    assert opened["diff"].endswith("@@ -1 +1,2 @@\n first\n+second\n")
    assert merged["merged"], merged["reason"]
    assert read_main(repositories) == "first\nsecond\n"


def test_a_branch_that_no_longer_merges_cleanly_leaves_main_as_it_is(
    open_forge: Callable[[ForgeSeed], Forge],
    repositories: Path,
) -> None:
    forge = open_forge(ForgeSeed())
    push(repositories, "docs/second", SECOND_LINE)
    push(repositories, "docs/another", OTHER_SECOND_LINE)
    for branch in ("docs/second", "docs/another"):
        forge.open_pr({"repo": "notes", "branch": branch, "actor_uid": "priya"})
    forge.merge({"repo": "notes", "number": 1, "actor_uid": "priya"})

    conflicting = forge.merge({"repo": "notes", "number": 2, "actor_uid": "priya"})

    assert not conflicting["merged"]
    assert "does not merge cleanly" in conflicting["reason"]
    assert read_main(repositories) == "first\nsecond\n"


def test_on_a_repo_needing_review_the_author_may_not_merge_their_own(
    open_forge: Callable[[ForgeSeed], Forge],
    repositories: Path,
) -> None:
    forge = open_forge(ForgeSeed(review_required=("notes",)))
    push(repositories, "docs/second", SECOND_LINE)
    forge.open_pr({"repo": "notes", "branch": "docs/second", "actor_uid": "priya"})

    by_author = forge.merge({"repo": "notes", "number": 1, "actor_uid": "priya"})
    by_teammate = forge.merge({"repo": "notes", "number": 1, "actor_uid": "omar"})

    assert (by_author["merged"], by_teammate["merged"]) == (False, True)


def test_a_pull_request_of_a_branch_never_pushed_is_refused(open_forge: Callable[[ForgeSeed], Forge]) -> None:
    forge = open_forge(ForgeSeed())

    with pytest.raises(ForgeError, match="push it first"):
        forge.open_pr({"repo": "notes", "branch": "docs/nowhere", "actor_uid": "priya"})
