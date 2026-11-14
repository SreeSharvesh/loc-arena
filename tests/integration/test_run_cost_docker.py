"""What a run cost, sampled from real containers: a compose project's, another project's, and a graded one.

Needs a reachable Docker daemon and the sandbox image; skipped otherwise.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator

import pytest
from loc_arena.agent_code import CONTAINER_NAME_PREFIX
from loc_arena.config import load_run_config
from loc_arena.run_cost import GRADING, PLAY, RunCost, record_cost, timed
from loc_arena.settings import CostSettings

from tests.integration._docker_support import image_exists

IMAGE = load_run_config("configs/aurora-efficiency.fast.yaml").settings.stack.sandbox_image
PROJECT = f"locarena-costtest{os.urandom(3).hex()}"  # its own, so two checkouts can run it at once
MEBIBYTE = 2**20
HELD, LIMIT, WRITTEN, BURNT_SECONDS = 96 * MEBIBYTE, 256 * MEBIBYTE, 3 * MEBIBYTE, 3
# Holds HELD bytes, writes WRITTEN to its volume, says it is ready, burns BURNT_SECONDS of CPU, says done.
WORK = f"""
import pathlib, time
held = bytearray({HELD})
held[::4096] = b"x" * len(held[::4096])
pathlib.Path("/data/blob").write_bytes(b"x" * {WRITTEN})
pathlib.Path("/data/ready").touch()
while time.process_time() < {BURNT_SECONDS}:
    pass
pathlib.Path("/data/done").touch()
time.sleep(600)
"""

pytestmark = pytest.mark.skipif(
    not image_exists(IMAGE),
    reason="needs a reachable Docker daemon and the sandbox image",
)


def run_docker(*arguments: str) -> str:
    done = subprocess.run(["docker", *arguments], capture_output=True, text=True, check=True, timeout=120)
    return done.stdout.strip()


def start_container(name: str, *options: str, command: str = "import time; time.sleep(600)") -> None:
    run_docker("run", "--detach", "--name", name, "--user", "0", *options, IMAGE, "python", "-c", command)


@pytest.fixture(scope="module")
def cost(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RunCost]:
    """The cost recorded over one made-up run: a working container of the project, then a graded one."""
    volume, worker, stranger = f"{PROJECT}_data", f"{PROJECT}-worker-1", f"{PROJECT}-stranger"
    graded = f"{CONTAINER_NAME_PREFIX}{PROJECT}"
    project = f"com.docker.compose.project={PROJECT}"
    labels = ["--label", project, "--label", "com.docker.compose.service=worker", "--memory", str(LIMIT)]
    run_directory = tmp_path_factory.mktemp("run")

    def wait_for(mark: str) -> None:
        while (
            run_docker("exec", worker, "sh", "-c", f"test -e /data/{mark} && echo there || true") != "there"
        ):
            time.sleep(0.2)

    @timed(PLAY)
    def play() -> None:
        wait_for("done")
        time.sleep(0.5)  # a sample of the worker once it has burnt its CPU

    @timed(GRADING)
    def grade() -> None:
        start_container(graded)
        time.sleep(1)

    try:
        run_docker("volume", "create", "--label", project, volume)
        with record_cost(CostSettings(sample_seconds=0.2), PROJECT) as recorder:
            start_container(worker, *labels, "--volume", f"{volume}:/data", command=WORK)
            start_container(stranger, "--label", "com.docker.compose.project=locarena-another")
            wait_for("ready")
            time.sleep(0.5)  # a sample of the worker at rest
            play()
            grade()
        yield recorder.write(run_directory, run_directory / "no-calls.jsonl")
    finally:
        for remove in (["rm", "--force", worker, stranger, graded], ["volume", "rm", "--force", volume]):
            subprocess.run(["docker", *remove], capture_output=True, check=False)


def test_a_container_of_the_project_has_its_memory_at_rest_at_peak_and_its_limit(cost: RunCost) -> None:
    worker = cost.containers["worker"]

    assert worker.memory_limit_bytes == LIMIT
    assert HELD <= worker.memory_peak_bytes < LIMIT
    assert worker.memory_rest_bytes is not None
    assert HELD <= worker.memory_rest_bytes <= worker.memory_peak_bytes


def test_a_container_has_the_cpu_seconds_it_burnt(cost: RunCost) -> None:
    assert BURNT_SECONDS <= cost.containers["worker"].cpu_seconds <= BURNT_SECONDS + 2  # and its start


def test_only_the_project_and_the_graded_containers_are_measured(cost: RunCost) -> None:
    assert sorted(cost.containers) == ["agent-code", "worker"]
    assert cost.containers["agent-code"].memory_rest_bytes is None  # it started after play
    assert cost.sampler.failed_samples == 0


def test_the_containers_together_peak_at_least_as_high_as_the_largest(cost: RunCost) -> None:
    assert cost.containers_rest_bytes is not None
    assert cost.containers_peak_bytes >= cost.containers["worker"].memory_peak_bytes
    assert cost.host_memory_peak_bytes is not None
    assert cost.host_memory_peak_bytes > cost.containers_peak_bytes


def test_each_image_and_each_volume_of_the_project_has_its_size(cost: RunCost) -> None:
    assert cost.images == {IMAGE: int(run_docker("image", "inspect", "--format", "{{.Size}}", IMAGE))}
    assert list(cost.volumes) == ["data"]
    assert WRITTEN <= cost.volumes["data"] < WRITTEN + MEBIBYTE


def test_a_recorded_run_has_its_wall_time_by_phase(cost: RunCost) -> None:
    assert list(cost.phases) == ["stack_start", "play", "copy", "grading", "teardown"]
    assert sum(cost.phases.values()) == pytest.approx(cost.wall_seconds)
