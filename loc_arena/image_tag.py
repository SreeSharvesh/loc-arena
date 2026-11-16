"""The tag of the images a checkout builds: ``latest``, or a linked git worktree's own."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


def derive_image_tag(checkout: Path) -> str:
    """The tag of the images built from ``checkout``: ``latest``, or a linked git worktree's own.

    Git gives a linked worktree a ``.git`` file where a clone has a folder. Its tag carries the folder's name
    and a digest of its path, so no two worktrees build over, or start, each other's images.
    """
    if not (checkout / ".git").is_file():
        return "latest"
    name = re.sub(r"[^\w.-]", "-", checkout.name, flags=re.ASCII)[:64]
    return f"latest-{name}-{hashlib.sha256(str(checkout).encode()).hexdigest()[:6]}"


IMAGE_TAG = derive_image_tag(Path(__file__).resolve().parents[1])  # of the checkout this code runs from
