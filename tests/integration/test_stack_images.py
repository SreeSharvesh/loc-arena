"""Each stack builds and runs images of its own, under its own tag, and its teardown removes them."""

from __future__ import annotations

import secrets
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from loc_arena.compose_document import APP_IMAGE, IMAGES, SANDBOX_IMAGE
from loc_arena.compose_stack import EpisodeStack, docker_available, run_compose, teardown, up
from loc_arena.config import load_run_config

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="docker daemon unavailable"),
]

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")


def _existing_images(stack: EpisodeStack) -> set[str]:
    references = {f"{name}:{stack.project}" for name in IMAGES.values()}
    return {
        reference
        for reference in references
        if subprocess.run(["docker", "image", "inspect", reference], capture_output=True).returncode == 0
    }


def _image_of_a_one_off_container(stack: EpisodeStack, service: str) -> str:
    name = f"{stack.project}-{service}-image-{secrets.token_hex(3)}"
    run_compose(stack, ["run", "--detach", "--name", name, service, "python", "-c", "pass"])
    try:
        inspected = subprocess.run(
            ["docker", "container", "inspect", name, "--format", "{{.Config.Image}}"],
            capture_output=True,
            text=True,
            check=True,
        )
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, check=False)
    return inspected.stdout.strip()


@pytest.fixture
def own_stack(tmp_path: Path) -> Iterator[EpisodeStack]:
    stack = up(CONFIG, project=f"locarena-images-{secrets.token_hex(3)}", workdir=tmp_path)
    try:
        yield stack
    finally:
        teardown(stack)


def test_teardown_removes_the_images_built_for_the_stack(own_stack: EpisodeStack) -> None:
    assert len(_existing_images(own_stack)) == len(IMAGES)  # guard: up built each image under the stack's tag

    teardown(own_stack)

    assert _existing_images(own_stack) == set()


def test_the_runner_runs_the_app_image_built_for_its_stack(stack: EpisodeStack) -> None:
    image = _image_of_a_one_off_container(stack, "runner")

    assert image == f"{APP_IMAGE}:{stack.project}"


def test_the_grader_runs_the_sandbox_image_built_for_its_stack(stack: EpisodeStack) -> None:
    image = _image_of_a_one_off_container(stack, "grader")

    assert image == f"{SANDBOX_IMAGE}:{stack.project}"
