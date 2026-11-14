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
from loc_arena.run_cost import GRADING, PLAY, RunCost, mark_phase, record_cost
from loc_arena.settings import CostSettings

from tests.integration._docker_support import image_exists

IMAGE = load_run_config("configs/aurora-efficiency.fast.yaml").settings.stack.sandbox_image
PROJECT = f"locarena-costtest{os.urandom(3).hex()}"  # its own, so two checkouts can run it at once
MEBIBYTE = 2**20
HELD, LIMIT, WRITTEN, BURNT_SECONDS = 96 * MEBIBYTE, 256 * MEBIBYTE, 3 * MEBIBYTE, 2
SERVICES_SECONDS = 1.0  # how long the made-up services take to start, before the episode does
SETTLE_SECONDS = 1.0  # several samples, at the interval below
# Holds HELD bytes, writes WRITTEN to /data, says it is ready, burns BURNT_SECONDS of CPU, says it is done.
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


def start_container(name: str, *options: str, command: str = "import time; time.sleep(600)") -> str:
    run_docker("run", "--detach", "--name", name, "--user", "0", *options, IMAGE, "python", "-c", command)
    return name


def start_service(service: str, *options: str, command: str = "import time; time.sleep(600)") -> str:
    """A container of the project, labelled as compose labels one; returns its name."""
    project, labelled = f"com.docker.compose.project={PROJECT}", f"com.docker.compose.service={service}"
    name = f"{PROJECT}-{service}-{os.urandom(2).hex()}"
    return start_container(name, "--label", project, "--label", labelled, *options, command=command)


def wait_for(container: str, mark: str) -> None:
    while run_docker("exec", container, "sh", "-c", f"test -e /data/{mark} && echo there || true") != "there":
        time.sleep(0.2)


@pytest.fixture(scope="module")
def cost(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RunCost]:
    """One made-up run: a worker, a service the renewal recreates, the episode, and a graded container."""
    volume, graded, stranger = f"{PROJECT}_data", f"{CONTAINER_NAME_PREFIX}{PROJECT}", f"{PROJECT}-stranger"
    started: list[str] = [stranger, graded]
    run_directory = tmp_path_factory.mktemp("run")

    @mark_phase(PLAY)
    def play() -> None:
        time.sleep(SERVICES_SECONDS)
        started.append(start_service("episode"))
        time.sleep(SETTLE_SECONDS)
        for _ in range(2):  # the service burns its CPU, is removed and recreated, and burns it again
            renewed = start_service("renewed", "--tmpfs", "/data", command=WORK)
            started.append(renewed)
            wait_for(renewed, "done")
            time.sleep(SETTLE_SECONDS)
            run_docker("rm", "--force", renewed)

    @mark_phase(GRADING)
    def grade() -> None:
        start_container(graded)
        time.sleep(SETTLE_SECONDS)

    try:
        run_docker("volume", "create", "--label", f"com.docker.compose.project={PROJECT}", volume)
        with record_cost(CostSettings(sample_seconds=0.2), PROJECT, "episode") as recorder:
            worker = start_service(
                "worker",
                "--memory",
                str(LIMIT),
                "--volume",
                f"{volume}:/data",
                command=WORK,
            )
            started.append(worker)
            start_container(stranger, "--label", "com.docker.compose.project=locarena-another")
            wait_for(worker, "done")
            play()
            grade()
        yield recorder.write(run_directory, run_directory / "no-calls.jsonl")
    finally:
        subprocess.run(["docker", "rm", "--force", *started], capture_output=True, check=False)
        subprocess.run(["docker", "volume", "rm", "--force", volume], capture_output=True, check=False)


def test_a_container_of_the_project_has_its_memory_at_rest_at_peak_and_its_limit(cost: RunCost) -> None:
    worker = cost.containers["worker"]

    assert worker.memory_limit_bytes == LIMIT
    assert HELD <= worker.memory_peak_bytes < LIMIT
    assert worker.memory_rest_bytes is not None
    assert HELD <= worker.memory_rest_bytes <= worker.memory_peak_bytes


def test_memory_at_rest_is_taken_as_the_episode_starts(cost: RunCost) -> None:
    assert cost.containers["episode"].memory_rest_bytes is not None
    assert cost.containers["agent-code"].memory_rest_bytes is None  # it started after
    assert cost.containers_rest_bytes is not None
    assert cost.containers_rest_bytes >= HELD  # the worker's, held since before


def test_a_container_has_the_cpu_seconds_it_burnt(cost: RunCost) -> None:
    assert BURNT_SECONDS <= cost.containers["worker"].cpu_seconds <= BURNT_SECONDS + 2  # and its start


def test_a_service_recreated_under_its_name_is_one_container_with_the_cpu_of_both(cost: RunCost) -> None:
    assert 2 * BURNT_SECONDS <= cost.containers["renewed"].cpu_seconds <= 2 * BURNT_SECONDS + 3


def test_only_the_project_and_the_graded_containers_are_measured(cost: RunCost) -> None:
    assert sorted(cost.containers) == ["agent-code", "episode", "renewed", "worker"]
    assert cost.sampler.failed_samples == 0


def test_the_containers_together_peak_at_least_as_high_as_the_largest(cost: RunCost) -> None:
    assert cost.containers_peak_bytes >= cost.containers["worker"].memory_peak_bytes
    assert cost.host_memory_peak_bytes is not None
    assert cost.host_memory_peak_bytes > cost.containers_peak_bytes


def test_each_image_and_each_volume_of_the_project_has_its_size(cost: RunCost) -> None:
    assert cost.images == {IMAGE: int(run_docker("image", "inspect", "--format", "{{.Size}}", IMAGE))}
    assert list(cost.volumes) == ["data"]
    assert WRITTEN <= cost.volumes["data"] < WRITTEN + MEBIBYTE


def test_the_services_start_is_its_own_phase_from_play_until_the_episode_starts(cost: RunCost) -> None:
    assert list(cost.phases) == ["stack_start", "services_start", "play", "copy", "grading", "teardown"]
    assert SERVICES_SECONDS <= cost.phases["services_start"] < SERVICES_SECONDS + SETTLE_SECONDS
