"""Per-PR CI status (deterministic).

The ``ci`` service runs an affected repo's suite on a pull request and reports a status the merge gate reads.
For the seeded world the status is a deterministic property of the PR (``ci_status``); for a live checkout the
real suite runs via ``run_repo_tests`` (no GPU, no network, just the repo's own pytest). CI is a tamperable,
agent-facing signal; it holds no oracle value.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Literal

from loc_arena.forge.world import PullRequest

CiStatus = Literal["green", "red", "pending"]


def ci_status(pr: PullRequest) -> CiStatus:
    """The deterministic CI status of a seeded-world PR (its declared status)."""
    status = pr.ci
    if status not in ("green", "red", "pending"):
        return "pending"
    return status  # type: ignore[return-value]


def run_repo_tests(checkout: Path, *, python_exe: str = sys.executable) -> CiStatus:
    """Run a repo checkout's own pytest suite and report ``green``/``red`` (used by the live PR path)."""
    result = subprocess.run(
        [python_exe, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )
    return "green" if result.returncode == 0 else "red"
