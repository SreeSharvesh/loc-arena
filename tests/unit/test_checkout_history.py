"""Each capture of an agent's checkout: git's own tree of it, only what changed stored, nothing followed."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from loc_arena.checkout_history import CheckoutHistory
from loc_arena.settings import HistorySettings

AGENT = "serving-agent"
REPO = "meridian-serving"
GIT = {"PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
COMMITTER = ["-c", "user.name=Serving Agent", "-c", "user.email=serving-agent@meridian.example"]


def git(clone: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *COMMITTER, *arguments],
        cwd=clone,
        env=GIT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """An agent's checkout holding one committed clone: a nested module, a script and a link."""
    clone = tmp_path / "checkout" / REPO
    (clone / "meridian_serving" / "cache").mkdir(parents=True)
    (clone / "meridian_serving" / "cache" / "swap.py").write_text("SWAP = 1\n")
    (clone / "run.sh").write_text("#!/bin/sh\n")
    (clone / "run.sh").chmod(0o755)
    (clone / "README.md").symlink_to("meridian_serving/cache/swap.py")
    git(clone, "init", "--quiet")
    git(clone, "add", "--all")
    git(clone, "commit", "--quiet", "-m", "Import")
    return clone.parent


def open_history(tmp_path: Path, checkout: Path, **settings: object) -> CheckoutHistory:
    return CheckoutHistory(
        tmp_path / "history",
        {AGENT: checkout},
        HistorySettings.model_validate(settings),
        lambda: 100.0,
    )


def count_objects(store: Path) -> int:
    return sum(1 for path in (store / "objects").glob("??/*"))


def test_a_repos_tree_in_a_capture_of_its_clean_clone_is_gits_own_tree_of_its_head(
    tmp_path: Path,
    checkout: Path,
) -> None:
    history = open_history(tmp_path, checkout)

    captured = history.capture(AGENT, "cloned")

    assert captured is not None
    assert captured["repos"] == {REPO: git(checkout / REPO, "rev-parse", "HEAD^{tree}")}


def test_a_capture_after_an_edit_stores_only_the_file_and_the_trees_above_it(
    tmp_path: Path,
    checkout: Path,
) -> None:
    history = open_history(tmp_path, checkout)
    history.capture(AGENT, "cloned")
    before = count_objects(tmp_path / "history")
    (checkout / REPO / "meridian_serving" / "cache" / "swap.py").write_text("SWAP = 2\n")

    history.capture(AGENT, "turn 1: write_file")

    # The edited file, the trees of cache/, meridian_serving/, the repo and the checkout, and the commit.
    assert count_objects(tmp_path / "history") - before == 6


def test_a_capture_of_an_unchanged_checkout_stores_nothing_and_adds_no_commit(
    tmp_path: Path,
    checkout: Path,
) -> None:
    history = open_history(tmp_path, checkout)
    first = history.capture(AGENT, "cloned")
    before = count_objects(tmp_path / "history")

    again = history.capture(AGENT, "turn 1: read_file")

    counted = ["git", "--git-dir", str(tmp_path / "history"), "rev-list", "--count", f"refs/agents/{AGENT}"]
    commits = subprocess.run(counted, env=GIT, capture_output=True, text=True, check=True).stdout.strip()
    assert (again, count_objects(tmp_path / "history") - before, commits) == (first, 0, "1")


def test_each_capture_is_a_commit_on_its_agents_ref_after_the_one_before(
    tmp_path: Path,
    checkout: Path,
) -> None:
    history = open_history(tmp_path, checkout)
    history.capture(AGENT, "cloned")
    (checkout / REPO / "NOTES.md").write_text("notes\n")

    captured = history.capture(AGENT, "turn 1: bash")

    store = tmp_path / "history"
    log = subprocess.run(
        ["git", "--git-dir", str(store), "log", "--format=%T %s", f"refs/agents/{AGENT}"],
        env=GIT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert captured is not None
    assert log[0] == f"{captured['tree']} turn 1: bash"
    assert [line.split(" ", 1)[1] for line in log] == ["turn 1: bash", "cloned"]


def test_a_link_out_of_the_checkout_is_stored_as_the_link_and_never_followed(
    tmp_path: Path,
    checkout: Path,
) -> None:
    secret = tmp_path / "sealed.jsonl"
    secret.write_text("a value no agent may read\n")
    (checkout / REPO / "leak").symlink_to(secret)
    history = open_history(tmp_path, checkout)

    history.capture(AGENT, "turn 1: bash")

    store = tmp_path / "history"
    shown = ["git", "--git-dir", str(store), "cat-file", "-p", f"refs/agents/{AGENT}:{REPO}/leak"]
    stored = subprocess.run(shown, env=GIT, capture_output=True, text=True, check=True).stdout
    every = ["git", "--git-dir", str(store), "grep", "--quiet", "no agent may read", f"refs/agents/{AGENT}"]
    assert stored == str(secret)
    assert subprocess.run(every, env=GIT, check=False).returncode == 1  # found nowhere in the capture


@pytest.mark.parametrize(
    ("make", "line"),
    [
        (lambda path: path.write_bytes(b"x" * 2048), "2048 bytes, over the 1024-byte limit"),
        (lambda path: (path.write_text("hidden\n"), path.chmod(0)), "7 bytes, unreadable"),
    ],
    ids=["over the limit", "unreadable"],
)
def test_a_file_left_out_is_stored_as_a_line_saying_why_and_named_in_the_event(
    tmp_path: Path,
    checkout: Path,
    make: object,
    line: str,
) -> None:
    make(checkout / REPO / "data.bin")  # ty: ignore[call-non-callable]
    history = open_history(tmp_path, checkout, max_file_bytes=1024)

    captured = history.capture(AGENT, "turn 1: bash")

    shown = ["git", "--git-dir", str(tmp_path / "history"), "show", f"refs/agents/{AGENT}:{REPO}/data.bin"]
    stored = subprocess.run(shown, env=GIT, capture_output=True, text=True, check=True).stdout
    assert captured is not None
    assert (stored, captured["omitted"]) == (
        f"left out of the checkout history: {line}\n",
        [f"{REPO}/data.bin"],
    )


def test_a_pipe_in_the_checkout_is_left_out_without_the_capture_waiting_on_it(
    tmp_path: Path,
    checkout: Path,
) -> None:
    os.mkfifo(checkout / REPO / "pipe")
    history = open_history(tmp_path, checkout)

    captured = history.capture(AGENT, "turn 1: bash")

    assert captured is not None
    assert captured["repos"] == {REPO: git(checkout / REPO, "rev-parse", "HEAD^{tree}")}


@pytest.mark.parametrize("bound", [{"max_entries": 3}, {"max_depth": 1}], ids=["entries", "depth"])
def test_a_capture_past_its_bounds_stops_and_says_it_was_cut_short(
    tmp_path: Path,
    checkout: Path,
    bound: dict[str, int],
) -> None:
    history = open_history(tmp_path, checkout, **bound)

    captured = history.capture(AGENT, "turn 1: bash")

    assert captured is not None
    assert captured["truncated"] is True


def test_an_agent_with_no_checkout_is_not_captured(tmp_path: Path, checkout: Path) -> None:
    history = open_history(tmp_path, checkout)

    assert history.capture("eval-agent", "turn 1: bash") is None
