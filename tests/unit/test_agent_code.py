"""Without the sandbox, agent code runs in this interpreter with the given PYTHONPATH and no provider key."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from loc_arena.agent_code import run_agent_code
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
