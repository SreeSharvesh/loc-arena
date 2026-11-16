"""What a run is, beside what it recorded: the run config it played and the commit of its engine.

A run's directory is named after its slug, which two configs share when one extends the other and keeps its
task, so ``run.json`` names the config itself. The commit is the checkout's, and ``modified`` says whether its
files differed from it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict

RUN_FILE = "run.json"  # in the run directory


class RunIdentity(BaseModel):
    """The run config a run played, by its name in ``configs/``, and the engine's commit, as ``run.json``."""

    model_config = ConfigDict(frozen=True)

    config: str
    commit: str
    modified: bool


def read_identity(config: str, repository: Path) -> RunIdentity:
    """The identity of a run of the config file ``config``, played from ``repository``, read before its build.

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
        return RunIdentity(config=_name(config), commit="unknown", modified=True)
    modified = bool(status.stdout.strip()) or tracked.returncode != 0
    return RunIdentity(config=_name(config), commit=commit.stdout.strip(), modified=modified)


def _name(config: str) -> str:
    return Path(config).name.removesuffix(".yaml")
