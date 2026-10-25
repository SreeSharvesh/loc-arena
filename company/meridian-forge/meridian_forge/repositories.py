"""The forge's git repositories: one bare repository per repo it hosts, each started from that repo's code.

Each repository's ``main`` starts as one commit of its repo's code, made with a fixed author, committer and
date and no user or system git configuration, so the same code gives the same commit ID every time, and a
recreated forge starts where the last one did.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

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


def seed_repositories(directory: Path, sources: Mapping[str, Path]) -> None:
    """Create a bare repository in ``directory`` for each repo of ``sources`` that has none, from its code.

    ``sources`` names each repo's code by the repo's name. A repository already there is left as it is, so a
    restarted forge keeps what was pushed and merged.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for repo, source in sources.items():
        bare = repository_path(directory, repo)
        if bare.exists():
            continue
        run_git(["init", "--quiet", "--bare", "--template=", f"--initial-branch={MAIN}", str(bare)])
        tree = ["--git-dir", str(bare), "--work-tree", str(source)]
        run_git([*tree, "add", "--all"])
        run_git([*tree, "commit", "--quiet", "-m", f"Import {repo}"], SEED_IDENTITY)
        (bare / "index").unlink()  # the staging area of the seed, which a bare repository keeps none of
