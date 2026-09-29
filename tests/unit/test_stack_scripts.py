"""The manual stack scripts, ``scripts/up.sh`` and the sweep ``scripts/teardown.sh``, on a stand-in docker.

The stand-in holds no resource at all, and refuses a compose project name compose itself would refuse.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest
from loc_arena.compose_stack import write_control_key_file
from loc_arena.harness import PROJECT_DIRECTORY

TEARDOWN_SCRIPT = PROJECT_DIRECTORY / "scripts" / "teardown.sh"
UP_SCRIPT = PROJECT_DIRECTORY / "scripts" / "up.sh"
# Lists nothing, removes nothing, and fails as compose does on a project name outside its rule
# (docs.docker.com/compose/how-tos/project-name: lowercase letters, digits, dashes and underscores, starting
# with a letter or a digit).
EMPTY_DOCKER = """#!/bin/sh
while [ $# -gt 0 ]; do
  if [ "$1" = "-p" ]; then
    printf '%s' "$2" | grep -Eq '^[a-z0-9][a-z0-9_-]*$' || { echo "invalid project name $2" >&2; exit 1; }
  fi
  shift
done
exit 0
"""


@pytest.fixture
def temporary_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The temp directory of the harness and the script alike, with a docker that holds nothing on PATH."""
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    docker = bin_directory / "docker"
    docker.write_text(EMPTY_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    monkeypatch.setenv("PATH", f"{bin_directory}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    return temporary


def _run_teardown_script() -> None:
    subprocess.run(["bash", str(TEARDOWN_SCRIPT)], check=True, capture_output=True)


def test_the_sweep_removes_a_control_key_its_harness_left_behind(temporary_directory: Path) -> None:
    key_file = write_control_key_file()

    _run_teardown_script()

    assert not key_file.parent.exists()


def test_the_sweep_leaves_other_temporary_directories(temporary_directory: Path) -> None:
    other = temporary_directory / "locarena-keep"
    other.mkdir()

    _run_teardown_script()

    assert other.is_dir()


def test_the_up_script_brings_the_deterministic_runs_stack_up(temporary_directory: Path) -> None:
    environment = {**os.environ, "RUN": "aurora-efficiency.deterministic"}

    result = subprocess.run(["bash", str(UP_SCRIPT)], env=environment, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
