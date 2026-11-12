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

from meridian_deploy.deployments import DeployError, MainTree
from meridian_forge.repositories import EXPORT_AS_COMMITTED, MAIN


@dataclass(frozen=True)
class ForgeSource:
    """``Source`` over the forge at ``url``: the ``main`` of each of ``repos``, each fetch within a bound."""

    url: str
    repos: Sequence[str]
    timeout_seconds: float

    def fetch(self, credential: str) -> dict[str, MainTree]:
        """Each repo's ``main`` as the forge holds it now, read with ``credential``."""
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {credential}",
        }
        with tempfile.TemporaryDirectory() as scratch:
            return {repo: self._fetch_repo(repo, Path(scratch) / repo, environment) for repo in self.repos}

    def _fetch_repo(self, repo: str, clone: Path, environment: dict[str, str]) -> MainTree:
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

        run_git(
            "clone",
            "--quiet",
            "--bare",
            "--depth",
            "1",
            "--branch",
            MAIN,
            f"{self.url}/git/{repo}",
            str(clone),
        )
        (clone / "info").mkdir(exist_ok=True)
        (clone / "info" / "attributes").write_text(EXPORT_AS_COMMITTED)
        commit = run_git("--git-dir", str(clone), "rev-parse", MAIN).decode().strip()
        return MainTree(commit, run_git("--git-dir", str(clone), "archive", "--format=tar", MAIN))
