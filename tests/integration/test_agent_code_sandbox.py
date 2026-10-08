"""With ``stack.sandbox_agent_code`` the grader's agent code runs in a throwaway container with no network.

Skipped unless the Docker daemon answers and the image named by ``stack.image`` exists.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest
from loc_arena.agent_code import CONTAINER_NAME_PREFIX, run_agent_code
from loc_arena.config import load_run_config
from loc_arena.settings import StackSettings
from loc_arena.tasks.main_task_grader import _suite_green, score_main_task, seed_checkout

DOCKER_CHECK_TIMEOUT_SECONDS = 10
HOST_ONLY_VARIABLE = "LOC_ARENA_TEST_HOST_ONLY"
SANDBOX = StackSettings(sandbox_agent_code=True)
CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
SANDBOXED_CONFIG = dataclasses.replace(CONFIG, settings=CONFIG.settings.model_copy(update={"stack": SANDBOX}))


def image_exists() -> bool:
    try:
        inspected = subprocess.run(
            ["docker", "image", "inspect", SANDBOX.image],
            capture_output=True,
            check=False,
            timeout=DOCKER_CHECK_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return inspected.returncode == 0


pytestmark = pytest.mark.skipif(
    not image_exists(),
    reason="needs a reachable Docker daemon and the stack image",
)


def run_in_sandbox(
    code: str,
    tmp_path: Path,
    *,
    timeout_seconds: float = 60,
    stack: StackSettings = SANDBOX,
) -> subprocess.CompletedProcess[str]:
    return run_agent_code(
        ["-c", code],
        working_directory=tmp_path,
        mount=tmp_path,
        pythonpath=str(tmp_path),
        timeout_seconds=timeout_seconds,
        stack=stack,
    )


def test_the_repo_suites_are_green_in_the_sandbox_on_a_fresh_checkout(tmp_path: Path) -> None:
    checkout = seed_checkout(tmp_path / "checkout")

    green = _suite_green(checkout, SANDBOX)

    assert green


def test_sandboxed_code_cannot_open_a_network_connection(tmp_path: Path) -> None:
    code = "import socket; socket.create_connection(('1.1.1.1', 53), 2)"

    result = run_in_sandbox(code, tmp_path)

    assert (result.returncode != 0, "unreachable" in result.stderr.lower()) == (True, True)


def test_a_timeout_removes_the_sandbox_container(tmp_path: Path) -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        run_in_sandbox("import time; time.sleep(30)", tmp_path, timeout_seconds=2)

    leftover = subprocess.run(
        ["docker", "ps", "--all", "--quiet", "--filter", f"name={CONTAINER_NAME_PREFIX}"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert leftover.stdout.strip() == ""


@pytest.mark.parametrize(
    ("limit_file", "stack", "expected"),
    [
        ("memory.max", StackSettings(sandbox_agent_code=True, episode_memory_limit="64m"), "67108864"),
        ("pids.max", StackSettings(sandbox_agent_code=True, episode_pids_limit=50), "50"),
        ("cpu.max", StackSettings(sandbox_agent_code=True, episode_cpus=0.5), "50000 100000"),
    ],
)
def test_the_sandbox_applies_the_episode_resource_limits(
    tmp_path: Path,
    limit_file: str,
    stack: StackSettings,
    expected: str,
) -> None:
    code = (
        "import os; "
        "print(open('/sys/fs/cgroup/" + limit_file + "').read().strip() "
        "if os.path.exists('/sys/fs/cgroup/cgroup.controllers') else 'cgroup-v1')"
    )

    result = run_in_sandbox(code, tmp_path, stack=stack)

    if result.stdout.strip() == "cgroup-v1":
        pytest.skip("the limit files read here exist only under cgroup v2")
    assert result.stdout.strip() == expected


def test_the_setting_moves_the_suites_and_the_benchmark_into_the_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = seed_checkout(tmp_path / "checkout")
    package = checkout / "meridian-common" / "meridian_common" / "__init__.py"
    raise_on_host = f"\nimport os\nif '{HOST_ONLY_VARIABLE}' in os.environ:\n    raise ImportError\n"
    package.write_text(package.read_text() + raise_on_host)
    monkeypatch.setenv(HOST_ONLY_VARIABLE, "1")  # a local run forwards it; the container gets only PYTHONPATH

    score = score_main_task(checkout, SANDBOXED_CONFIG)

    assert (score.gate_passed, score.gate_reason) == (True, "gate_passed")
