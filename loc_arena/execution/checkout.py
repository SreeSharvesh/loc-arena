"""The company checkout the agents work on: which repositories it holds, how its code runs, how it is copied.

A checkout is the seven ``company/`` repositories side by side (``meridian-common/``, ...); they import when
each repository directory is on ``PYTHONPATH``. Their code runs on the plain interpreter, never ``uv run``
(uv cannot resolve the copied repositories' dependencies). The host (STACK=0), each agent's sandbox and the
grader build and run a checkout with this one module, which ships in the sandbox image.
"""

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
# Caches and history: never content, so never listed, searched or copied.
IGNORED_NAMES: Final = frozenset({"__pycache__", ".pytest_cache", ".git"})
SUITE_ARGUMENTS: Final = ("-m", "pytest", "-q", "-p", "no:cacheprovider")  # one repository's suite, no cache


def list_repositories(root: Path) -> tuple[str, ...]:
    """The names of the company repositories directly under ``root``, sorted."""
    names = (
        path.name for path in root.iterdir() if path.is_dir() and path.name.startswith(REPOSITORY_PREFIX)
    )
    return tuple(sorted(names))


def copy_repositories(source: Path, destination: Path, repositories: Iterable[str]) -> None:
    """Copy each named repository of ``source`` into ``destination``, leaving out caches and history.

    A symlink is copied as a link and never followed, so a link an agent planted cannot make the copy read
    outside the checkout. Raises ``shutil.Error`` (an ``OSError``) when a file cannot be copied, such as a
    named pipe, and ``FileNotFoundError`` when a repository is missing.
    """
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
    """A checkout on disk: its root, the repositories its code imports, and the interpreter that runs it."""

    root: Path
    repositories: tuple[str, ...]
    python_executable: str = sys.executable

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
        """Run the interpreter with ``arguments`` in ``cwd``, keeping the end of each output stream.

        Killed with its process group at ``timeout_seconds`` (then ``exit_code`` is ``None``); a process it
        detaches never holds the call (:func:`~loc_arena.execution.command.run_command`).
        """
        return run_command(
            (self.python_executable, *arguments),
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
            SUITE_ARGUMENTS,
            cwd=self.root / repository,
            timeout_seconds=timeout_seconds,
            max_output_characters=max_output_characters,
        )
