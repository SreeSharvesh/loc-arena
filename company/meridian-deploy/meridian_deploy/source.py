"""The forge's ``main`` of each company repo, fetched over git's HTTP protocol with a deployment's credential.

Each repo is cloned bare and one commit deep, so no working tree of the fetched code is ever checked out
here, and git archives its ``main`` as committed: the clone's own attributes override any ``.gitattributes``
the tree brings, as the forge's repositories do for grading.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from meridian_deploy.deployments import DeployError, MainTree
from meridian_forge.repositories import EXPORT_AS_COMMITTED, MAIN


@dataclass(frozen=True)
class ForgeSource:
    """``Source`` over the forge at ``url``: the ``main`` of each of ``repos``, within bounds.

    Each fetch from the forge ends within ``timeout_seconds``, and the trees together hold at most
    ``max_bytes`` as tars: a larger ``main`` is refused before more of it is read.
    """

    url: str
    repos: Sequence[str]
    timeout_seconds: float
    max_bytes: int

    def fetch(self, credential: SecretStr) -> dict[str, MainTree]:
        """Each repo's ``main`` as the forge holds it now, read with ``credential``."""
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {credential.get_secret_value()}",
        }
        trees: dict[str, MainTree] = {}
        with tempfile.TemporaryDirectory() as scratch:
            for repo in self.repos:
                left = self.max_bytes - sum(len(tree.archive) for tree in trees.values())
                trees[repo] = self._fetch_repo(repo, Path(scratch) / repo, environment, left)
        return trees

    def _fetch_repo(self, repo: str, clone: Path, environment: dict[str, str], max_bytes: int) -> MainTree:
        def run_git(*arguments: str) -> bytes:
            try:
                done = subprocess.run(  # noqa: S603 - git, on arguments this module writes
                    ["git", *arguments],  # noqa: S607 - the image's git
                    capture_output=True,
                    check=False,
                    env=environment,
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired as late:
                raise DeployError(f"the forge did not send {repo} in {self.timeout_seconds:g} s") from late
            if done.returncode != 0:
                raise DeployError(f"cannot fetch {repo} from the forge: {done.stderr.decode().strip()}")
            return done.stdout

        bare = ["clone", "--quiet", "--bare", "--depth", "1", "--branch", MAIN]
        run_git(*bare, f"{self.url}/git/{repo}", str(clone))
        (clone / "info").mkdir(exist_ok=True)
        (clone / "info" / "attributes").write_text(EXPORT_AS_COMMITTED)
        commit = run_git("--git-dir", str(clone), "rev-parse", MAIN).decode().strip()
        archive = ["git", "--git-dir", str(clone), "archive", "--format=tar", MAIN]
        # Read as it is written, and no further than the bound: a tree can unpack far larger than its clone.
        with subprocess.Popen(archive, stdout=subprocess.PIPE, env=environment) as archiving:  # noqa: S603
            assert archiving.stdout is not None  # noqa: S101 - opened with a pipe
            tree = archiving.stdout.read(max_bytes + 1)
            if len(tree) > max_bytes:
                archiving.kill()
                raise DeployError(f"main is larger than a release may be: {self.max_bytes} bytes of trees")
        if archiving.returncode != 0:
            raise DeployError(f"cannot archive {repo}'s main")
        return MainTree(commit, tree)
