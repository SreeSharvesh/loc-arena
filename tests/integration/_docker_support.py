"""Whether the Docker tests can run: a reachable daemon and the stack image."""

from __future__ import annotations

import subprocess

from loc_arena.settings import StackSettings

DOCKER_CHECK_TIMEOUT_SECONDS = 10


def image_exists(image: str = StackSettings().image) -> bool:
    """Whether the Docker daemon answers and holds ``image``."""
    try:
        inspected = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            check=False,
            timeout=DOCKER_CHECK_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return inspected.returncode == 0
