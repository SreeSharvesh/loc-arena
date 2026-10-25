"""The forge's git repositories: one bare repository per company repo, seeded with the company's code.

Each repository's ``main`` starts as one commit of its company repo, made with a fixed author, committer and
date and no user or system git configuration, so the same code gives the same commit ID in every run, and the
honest twin's recreated forge starts where the attack's did.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

from loc_arena.tasks.main_task_grader import COMPANY_REPOS, seed_checkout

MAIN = "main"
REPOSITORY_SUFFIX = ".git"  # a bare repository's directory is its repo's name with git's own suffix
SEED_IDENTITY = {
    "GIT_AUTHOR_NAME": "Meridian Engineering",
    "GIT_AUTHOR_EMAIL": "engineering@meridian.example",
    "GIT_AUTHOR_DATE": "2026-01-05T09:00:00+00:00",
    "GIT_COMMITTER_NAME": "Meridian Engineering",
    "GIT_COMMITTER_EMAIL": "engineering@meridian.example",
    "GIT_COMMITTER_DATE": "2026-01-05T09:00:00+00:00",
}
# Git configuration from neither the user nor the system: no hook, template or signing setting changes a seed.
ISOLATED_GIT = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}


def run_git(arguments: list[str], environment: Mapping[str, str] | None = None) -> str:
    """Run ``git`` with ``arguments`` under the isolated configuration and return its stdout."""
    result = subprocess.run(
        ["git", *arguments],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ["PATH"], **ISOLATED_GIT, **(environment or {})},
    )
    return result.stdout


def repository_path(directory: Path, repo: str) -> Path:
    """Where ``repo``'s bare repository is in ``directory``."""
    return directory / f"{repo}{REPOSITORY_SUFFIX}"


def seed_repositories(directory: Path) -> None:
    """Create a bare repository in ``directory`` for each company repo that has none, its ``main`` the seed.

    A repository already there is left as it is, so a restarted forge keeps what was pushed and merged.
    """
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        checkout = seed_checkout(Path(scratch) / "checkout")
        for repo in COMPANY_REPOS:
            bare = repository_path(directory, repo)
            if bare.exists():
                continue
            run_git(["init", "--quiet", "--bare", "--template=", f"--initial-branch={MAIN}", str(bare)])
            tree = ["--git-dir", str(bare), "--work-tree", str(checkout / repo)]
            run_git([*tree, "add", "--all"])
            run_git([*tree, "commit", "--quiet", "-m", f"Import {repo}"], SEED_IDENTITY)
            (bare / "index").unlink()  # the staging area of the seed, which a bare repository keeps none of
