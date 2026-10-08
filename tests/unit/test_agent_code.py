"""Without the sandbox, agent code runs in this interpreter with the given PYTHONPATH and no provider key."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from loc_arena.agent_code import CONTAINER_NAME_PREFIX, run_agent_code
from loc_arena.gateway.core import API_KEY_VARIABLE
from loc_arena.settings import StackSettings


def run_locally(
    code: str,
    tmp_path: Path,
    *,
    timeout_seconds: float = 30,
) -> subprocess.CompletedProcess[str]:
    return run_agent_code(
        ["-c", code],
        working_directory=tmp_path,
        mount=tmp_path,
        pythonpath="/the/pythonpath",
        timeout_seconds=timeout_seconds,
        stack=StackSettings(sandbox_agent_code=False),
    )


def test_the_given_pythonpath_and_working_directory_reach_the_code(tmp_path: Path) -> None:
    code = "import os; print(os.environ['PYTHONPATH'], os.getcwd())"

    result = run_locally(code, tmp_path)

    assert result.stdout.split() == ["/the/pythonpath", str(tmp_path.resolve())]


def test_the_provider_key_does_not_reach_the_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_VARIABLE, "a-secret")

    result = run_locally(f"import os; print(os.environ.get('{API_KEY_VARIABLE}', 'absent'))", tmp_path)

    assert result.stdout.strip() == "absent"


def test_a_timeout_raises_timeout_expired(tmp_path: Path) -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        run_locally("import time; time.sleep(30)", tmp_path, timeout_seconds=0.5)


def test_output_beyond_the_limit_is_cut_from_the_front_keeping_the_last_line(tmp_path: Path) -> None:
    stack = StackSettings(agent_code_output_limit_bytes=100)

    result = run_agent_code(
        ["-c", "print('x' * 5000); print('the last line')"],
        working_directory=tmp_path,
        mount=tmp_path,
        pythonpath="",
        timeout_seconds=30,
        stack=stack,
    )

    assert (len(result.stdout), result.stdout.endswith("the last line\n")) == (100, True)


def docker_stub(directory: Path, *, stops: bool) -> Path:
    """A `docker` that hangs on `run` and `kill`, and on `rm` unless ``stops``; a real executable on PATH."""
    stub = directory / "docker"
    stub.write_text(f'#!/bin/sh\n[ "$1" = rm ] && {"exit 0" if stops else "exec sleep 30"}\nexec sleep 30\n')
    stub.chmod(0o755)
    return directory


def run_with_hung_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, stops: bool) -> None:
    monkeypatch.setenv("PATH", f"{docker_stub(tmp_path, stops=stops)}:{os.environ['PATH']}")
    monkeypatch.setattr("loc_arena.agent_code.CLEANUP_TIMEOUT_SECONDS", 0.5)
    run_agent_code(
        ["-c", "pass"],
        working_directory=tmp_path,
        mount=tmp_path,
        pythonpath="",
        timeout_seconds=0.5,
        stack=StackSettings(sandbox_agent_code=True),
    )


def test_a_container_that_kill_cannot_stop_is_force_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        run_with_hung_docker(tmp_path, monkeypatch, stops=True)


def test_a_container_that_nothing_can_stop_is_named_in_the_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match=CONTAINER_NAME_PREFIX) as raised:
        run_with_hung_docker(tmp_path, monkeypatch, stops=False)

    assert isinstance(raised.value.__cause__, subprocess.TimeoutExpired)
