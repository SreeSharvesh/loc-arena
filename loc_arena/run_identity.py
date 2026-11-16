"""The engine a run played on: its checkout's commit, and whether the checkout differed from it.

``scores.json`` names the run's config; ``run.json`` names the commit, so a run can be played again from it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict

RUN_FILE = "run.json"  # in the run directory


class RunIdentity(BaseModel):
    """The engine's commit a run played on, and whether its checkout differed from it, as ``run.json``."""

    model_config = ConfigDict(frozen=True)

    commit: str
    modified: bool


def read_identity(config: str, repository: Path) -> RunIdentity:
    """The engine a run of the config file ``config`` plays on, from ``repository``, read before its build.

    The run is modified when the checkout differs from its commit, or the config is not in it, as a local one
    is not. A commit git cannot read, or a missing git, records an unknown commit, counted as modified.
    """
    git = ["git", "-C", str(repository)]
    try:
        commit = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
        status = subprocess.run([*git, "status", "--porcelain"], capture_output=True, text=True, check=True)
        tracked = subprocess.run(
            [*git, "ls-files", "--error-unmatch", config],
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.CalledProcessError):
        return RunIdentity(commit="unknown", modified=True)
    modified = bool(status.stdout.strip()) or tracked.returncode != 0
    return RunIdentity(commit=commit.stdout.strip(), modified=modified)
