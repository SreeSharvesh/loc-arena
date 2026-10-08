"""Agent-written code: the one place it is run, on this host or in a throwaway container with no network."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from loc_arena.gateway.core import environment_without_key
from loc_arena.settings import StackSettings

CONTAINER_NAME_PREFIX = "locarena-agent-code-"


def run_agent_code(
    arguments: Sequence[str],
    *,
    working_directory: Path,
    mount: Path,
    pythonpath: str,
    timeout_seconds: float,
    stack: StackSettings,
) -> subprocess.CompletedProcess[str]:
    """Run ``python <arguments>`` over code the agents wrote and return the finished process.

    With ``stack.sandbox_agent_code`` the process runs in a throwaway container of ``stack.image``: no
    network, no capabilities, the host user's uid and gid, and only ``mount`` visible, at the same absolute
    path so ``pythonpath`` stays valid. Otherwise it runs in this process's interpreter with the provider key
    removed from its environment. A timeout kills the container, so a hung process never outlives its caller,
    and re-raises ``subprocess.TimeoutExpired``.
    """
    if not stack.sandbox_agent_code:
        return subprocess.run(
            [sys.executable, *arguments],
            cwd=working_directory,
            env={**environment_without_key(), "PYTHONPATH": pythonpath},
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    container_name = f"{CONTAINER_NAME_PREFIX}{uuid.uuid4().hex}"
    try:
        return subprocess.run(
            [
                "docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
                "--name", container_name,
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--volume", f"{mount}:{mount}",
                "--workdir", str(working_directory),
                "--env", f"PYTHONPATH={pythonpath}",
                stack.image, "python", *arguments,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", container_name], capture_output=True, check=False)
        raise
