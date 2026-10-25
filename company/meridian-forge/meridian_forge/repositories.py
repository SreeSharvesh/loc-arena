"""The forge's git repositories: one bare repository per repo it hosts, each started from that repo's code.

Each repository's ``main`` starts as one commit of its repo's code, made with a fixed author, committer and
date and no user or system git configuration, so the same code gives the same commit ID every time, and a
recreated forge starts where the last one did. Each pull request it starts with is a branch off ``main`` with
one commit, its change, by its author at the time it was opened. ``main`` is protected: a hook refuses every
push to it, so it changes only by the forge's own merge (``merge_branch``).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meridian_forge.seed import SeedPullRequest

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
EMAIL_DOMAIN = "meridian.example"  # a principal's commits carry <principal>@ this domain
# The hook each repository runs on a push: main changes only by a merge on the forge.
PROTECT_MAIN = f"""#!/bin/sh
while read -r old new ref; do
  if [ "$ref" = "refs/heads/{MAIN}" ]; then
    echo "{MAIN} is protected: push a branch and open a pull request" >&2
    exit 1
  fi
done
"""


def run_git(arguments: list[str], environment: Mapping[str, str] | None = None, *, check: bool = True) -> str:
    """Run ``git`` with ``arguments`` under the isolated configuration and return its stdout.

    A failure raises ``CalledProcessError`` unless ``check`` is off.
    """
    result = subprocess.run(
        ["git", *arguments],
        capture_output=True,
        text=True,
        check=check,
        env={"PATH": os.environ["PATH"], **ISOLATED_GIT, **(environment or {})},
    )
    return result.stdout if result.returncode == 0 else ""


def identity(principal: str, when: datetime | None = None) -> dict[str, str]:
    """Git's author and committer environment for ``principal``, at ``when`` or the present."""
    fields = {"NAME": principal, "EMAIL": f"{principal}@{EMAIL_DOMAIN}"}
    if when is not None:
        fields["DATE"] = when.isoformat()
    return {
        f"GIT_{role}_{field}": value for role in ("AUTHOR", "COMMITTER") for field, value in fields.items()
    }


def repository_path(directory: Path, repo: str) -> Path:
    """Where ``repo``'s bare repository is in ``directory``."""
    return directory / f"{repo}{REPOSITORY_SUFFIX}"


def seed_repositories(
    directory: Path,
    sources: Mapping[str, Path],
    branches: Iterable[SeedPullRequest] = (),
) -> None:
    """Create a bare repository in ``directory`` for each repo of ``sources`` that has none, and its branches.

    ``sources`` names each repo's code by the repo's name; ``branches`` are the branches to start with. A
    repository already there is left as it is, so a restarted forge keeps what was pushed and merged.
    """
    directory.mkdir(parents=True, exist_ok=True)
    unknown = sorted({branch.repo for branch in branches} - set(sources))
    if unknown:
        raise ValueError(f"branches on repos the forge does not host: {unknown}")
    for repo, source in sources.items():
        bare = repository_path(directory, repo)
        if bare.exists():
            continue
        run_git(["init", "--quiet", "--bare", "--template=", f"--initial-branch={MAIN}", str(bare)])
        tree = ["--git-dir", str(bare), "--work-tree", str(source)]
        run_git([*tree, "add", "--all"])
        run_git([*tree, "commit", "--quiet", "-m", f"Import {repo}"], SEED_IDENTITY)
        (bare / "index").unlink()  # the staging area of the seed, which a bare repository keeps none of
        hooks = bare / "hooks"
        hooks.mkdir()
        (hooks / "pre-receive").write_text(PROTECT_MAIN)
        (hooks / "pre-receive").chmod(0o755)
        for branch in branches:
            if branch.repo == repo:
                commit_branch(
                    directory,
                    repo,
                    branch.branch,
                    branch.patch,
                    branch.title,
                    identity(branch.author, branch.opened_at),
                )
        run_git(["--git-dir", str(bare), "repack", "-a", "-d", "-q"])  # one pack, which a clone hardlinks


def commit_branch(  # noqa: PLR0913 - a commit's every part
    directory: Path,
    repo: str,
    branch: str,
    patch: str,
    message: str,
    author: Mapping[str, str],
) -> None:
    """Commit ``patch`` onto ``main`` of ``repo`` as the new ``branch`` by ``author``, with no work tree."""
    git = ["--git-dir", str(repository_path(directory, repo))]
    with tempfile.TemporaryDirectory() as scratch:
        index, change = Path(scratch) / "index", Path(scratch) / "change.patch"
        change.write_text(patch)
        staging = {"GIT_INDEX_FILE": str(index)}
        run_git([*git, "read-tree", MAIN], staging)
        run_git([*git, "apply", "--cached", str(change)], staging)
        tree = run_git([*git, "write-tree"], staging).strip()
    commit = run_git([*git, "commit-tree", tree, "-p", MAIN, "-m", message], author).strip()
    run_git([*git, "update-ref", f"refs/heads/{branch}", commit])


def branch_exists(directory: Path, repo: str, branch: str) -> bool:
    """Whether ``repo`` has ``branch``."""
    git = ["--git-dir", str(repository_path(directory, repo))]
    return bool(run_git([*git, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], check=False))


def branch_diff(directory: Path, repo: str, branch: str) -> str:
    """What ``branch`` of ``repo`` changes since it left ``main``, as git shows it."""
    return run_git(["--git-dir", str(repository_path(directory, repo)), "diff", f"{MAIN}...{branch}"])


def merge_branch(directory: Path, repo: str, branch: str, message: str, merger: str) -> bool:
    """Merge ``branch`` onto ``main`` of ``repo`` as ``merger`` with git's own merge; ``False`` on a conflict.

    The merge commit is written without a working tree, and ``main`` moves only if no one moved it meanwhile.
    """
    git = ["--git-dir", str(repository_path(directory, repo))]
    base = run_git([*git, "rev-parse", MAIN]).strip()
    head = run_git([*git, "rev-parse", f"refs/heads/{branch}"]).strip()
    tree = run_git([*git, "merge-tree", "--write-tree", base, head], check=False).split("\n", 1)[0]
    if not tree:
        return False
    commit = run_git(
        [*git, "commit-tree", tree, "-p", base, "-p", head, "-m", message],
        identity(merger),
    ).strip()
    run_git([*git, "update-ref", f"refs/heads/{MAIN}", commit, base])
    return True
