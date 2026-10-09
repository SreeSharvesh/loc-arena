"""Where a path may point: inside a directory, with links and ``..`` resolved first."""

from __future__ import annotations

from pathlib import Path


def resolve_inside(directory: Path, stored: Path) -> Path:
    """``stored`` resolved against ``directory``; refused outside it, since what it points at gets run."""
    resolved = (directory / stored).resolve()
    if not resolved.is_relative_to(directory.resolve()):
        raise ValueError(f"{stored} is outside {directory}")
    return resolved
