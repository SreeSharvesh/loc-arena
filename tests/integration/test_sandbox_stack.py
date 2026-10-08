"""In a stack run, bash runs in the sandbox: the checkout, not the sealed log, and nothing outlives a command.

The rendered compose project is brought up for real; inside the episode container, the agents' code tools
run a command through the sandbox client as a played episode does. Skipped unless the Docker daemon answers
and the stack image exists; the image is rebuilt first, so it holds this code.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from typing import Any

import pytest
import yaml
from loc_arena.config import load_run_config
from loc_arena.episode_stack import REPOSITORY, render_compose
from loc_arena.gateway.core import API_KEY_VARIABLE
from loc_arena.sandbox import TOKEN_VARIABLE

from tests.integration._docker_support import image_exists

pytestmark = pytest.mark.skipif(
    not image_exists(),
    reason="needs a reachable Docker daemon and the stack image",
)

RUN = "aurora-efficiency.deterministic"
CHECKOUT_REPOS = [
    "meridian-common",
    "meridian-controlplane",
    "meridian-datapipe",
    "meridian-distill",
    "meridian-evalkit",
    "meridian-jobsvc",
    "meridian-serving",
]
# Run in the episode container: a sealed log where play writes one, a checkout where play seeds one, and the
# agents' bash asked to read the one, to list the other, and to leave a process running and look for it.
IN_EPISODE = f"""
import json
from pathlib import Path
from loc_arena.config import load_run_config
from loc_arena.sandbox import connect_sandbox
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.scaffold.tools import StubServices
from loc_arena.task import seed_episode_checkout

config = load_run_config("/app/configs/{RUN}.yaml")
episode = Path("/output/a-run/episode")
episode.mkdir(parents=True)
sealed = episode / "events.sealed.jsonl"
sealed.write_text("sealed\\n")
services = CodeServices(
    StubServices(),
    checkout=seed_episode_checkout(config, episode),
    repos=[],
    stack=config.settings.stack,
    sandbox=connect_sandbox(config.settings),
)
results = [services.run("bash", {{"command": command}}) for command in (f"cat {{sealed}}", "ls")]
left = services.run("bash", {{"command": "setsid sleep 300 > /dev/null 2>&1 < /dev/null & echo $!"}})
results.append(services.run("bash", {{"command": f"kill -0 {{left['stdout'].strip()}}"}}))
print(json.dumps(results))
"""


@pytest.fixture(scope="module")
def bash_results(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    directory = tmp_path_factory.mktemp("stack")
    compose_file = directory / "compose.yaml"
    config = load_run_config(REPOSITORY / "configs" / f"{RUN}.yaml")
    compose_file.write_text(yaml.safe_dump(render_compose(config, REPOSITORY, RUN, [])))
    compose = ["docker", "compose", "-p", f"locarena-test-{secrets.token_hex(3)}", "-f", str(compose_file)]
    # The project directory holds no .env, so the gateway gets this placeholder and never the real key.
    compose += ["--project-directory", str(directory)]
    environment = {
        **os.environ,
        API_KEY_VARIABLE: "dummy-for-offline-run",
        TOKEN_VARIABLE: secrets.token_urlsafe(32),
    }
    try:
        played = subprocess.run(
            [*compose, "run", "--rm", "--build", "episode", "python", "-c", IN_EPISODE],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
    finally:
        down = [*compose, "down", "--volumes", "--remove-orphans"]
        subprocess.run(down, env=environment, capture_output=True, check=False)
    return json.loads(played.stdout.strip().splitlines()[-1])


def test_bash_in_a_stack_run_cannot_read_the_episodes_sealed_log(bash_results: list[dict[str, Any]]) -> None:
    read = bash_results[0]

    missing = "No such file or directory" in read["stderr"]
    assert (read["returncode"], read["stdout"], missing) == (1, "", True)


def test_bash_in_a_stack_run_sees_the_seeded_checkout(bash_results: list[dict[str, Any]]) -> None:
    listed = bash_results[1]

    expected = "".join(f"{repo}\n" for repo in CHECKOUT_REPOS)
    assert listed == {"returncode": 0, "stdout": expected, "stderr": ""}


def test_a_process_a_bash_command_leaves_running_ends_with_it(bash_results: list[dict[str, Any]]) -> None:
    looked_for = bash_results[2]

    gone = "No such process" in looked_for["stderr"]
    assert (looked_for["returncode"], gone) == (1, True)
