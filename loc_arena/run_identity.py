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
    """The run config a run played, by its name in ``configs/``, and the engine's commit."""

    model_config = ConfigDict(frozen=True)

    config: str
    commit: str
    modified: bool


def record_identity(run_directory: Path, config: str, repository: Path) -> RunIdentity:
    """Write ``run.json`` for the run in ``run_directory``, played with ``config`` from ``repository``."""
    git = ["git", "-C", str(repository)]
    commit = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    status = subprocess.run([*git, "status", "--porcelain"], capture_output=True, text=True, check=False)
    identity = RunIdentity(
        config=Path(config).name.removesuffix(".yaml"),
        commit=commit.stdout.strip() if commit.returncode == 0 else "unknown",
        modified=status.returncode != 0 or bool(status.stdout.strip()),
    )
    (run_directory / RUN_FILE).write_text(identity.model_dump_json(indent=1))
    return identity
