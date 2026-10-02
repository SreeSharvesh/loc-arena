"""The company checkout: which repositories it holds, how its code runs, how it is copied."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from loc_arena.execution.command import CommandResult, run_command

# The pristine repositories, next to the package: company/ in the repository, /app/company in both images.
COMPANY_ROOT: Final = Path(__file__).resolve().parents[2] / "company"
REPOSITORY_PREFIX: Final = "meridian-"
IGNORED_NAMES: Final = frozenset({"__pycache__", ".pytest_cache", ".git"})


def list_repositories(root: Path) -> tuple[str, ...]:
    """The names of the company repositories directly under ``root``, sorted."""
    names = (
        path.name for path in root.iterdir() if path.is_dir() and path.name.startswith(REPOSITORY_PREFIX)
    )
    return tuple(sorted(names))


def copy_repositories(source: Path, destination: Path, repositories: Iterable[str]) -> None:
    """Copy each named repository of ``source`` into ``destination``, symlinks as links, caches left out."""
    for repository in repositories:
        shutil.copytree(
            source / repository,
            destination / repository,
            symlinks=True,
            ignore=shutil.ignore_patterns(*IGNORED_NAMES),
            dirs_exist_ok=True,
        )


def extract_last_line(text: str) -> str:
    """The last non-blank line a process printed ("" when it printed none): where a report or summary is."""
    lines = text.strip().splitlines()
    return lines[-1] if lines else ""


@dataclass(frozen=True)
class Checkout:
    """A checkout on disk: its root and the repositories its code imports."""

    root: Path
    repositories: tuple[str, ...]

    def build_environment(self) -> dict[str, str]:
        """This process's environment with every repository of the checkout importable."""
        python_path = os.pathsep.join(str(self.root / repository) for repository in self.repositories)
        return {**os.environ, "PYTHONPATH": python_path}

    def run_python(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_output_characters: int,
    ) -> CommandResult:
        """Run the interpreter with ``arguments`` in ``cwd``, keeping the end of each output stream."""
        return run_command(
            (sys.executable, *arguments),
            cwd=cwd,
            environment=self.build_environment(),
            timeout_seconds=timeout_seconds,
            max_output_characters=max_output_characters,
        )

    def run_suite(
        self,
        repository: str,
        *,
        timeout_seconds: float,
        max_output_characters: int,
    ) -> CommandResult:
        """Run one repository's own test suite, as :meth:`run_python` runs the interpreter."""
        return self.run_python(
            ("-m", "pytest", "-q", "-p", "no:cacheprovider"),
            cwd=self.root / repository,
            timeout_seconds=timeout_seconds,
            max_output_characters=max_output_characters,
        )
