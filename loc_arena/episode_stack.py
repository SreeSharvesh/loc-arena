"""One episode as a compose project: the gateway, and the episode in a container with no key and no internet.

The gateway is the only container with the provider key and a route out (agent-net and egress-net). The
episode runs ``loc_arena.cli run`` on agent-net alone, so its model calls can only go to the gateway. When the
episode exits, its audit bundle and the gateway's call log are copied out and the project is removed with its
volumes. If a copy fails, the project is kept so nothing recorded is lost.
"""

from __future__ import annotations

import secrets
import subprocess
from pathlib import Path, PurePosixPath
from typing import TypedDict

import yaml

from loc_arena.config import load_settings
from loc_arena.gateway.core import API_KEY_VARIABLE, GATEWAY_URL_VARIABLE
from loc_arena.settings import LocArenaSettings

AGENT_NETWORK = "agent-net"  # the episode and the gateway, with no route out
EGRESS_NETWORK = "egress-net"  # the gateway alone: its route to the provider
KEY_SECRET = "openrouter_api_key"  # the compose secret, a file in the gateway's settings.gateway.secrets_dir
OUTPUT_DIRECTORY = PurePosixPath("/output")  # the episode's audit bundles; the image creates it for nobody
CONFIGS_DIRECTORY = PurePosixPath("/app/configs")  # the run configs, mounted read-only
CHAT_COMPLETIONS = "api/v1/chat/completions"


class Healthcheck(TypedDict):
    """A compose healthcheck."""

    test: list[str]
    interval: str
    retries: int


class ComposeService(TypedDict, total=False):
    """The compose keys a rendered service uses."""

    build: str
    image: str
    command: list[str]
    environment: dict[str, str]
    secrets: list[str]
    volumes: list[str]
    networks: list[str]
    cap_drop: list[str]
    depends_on: dict[str, dict[str, str]]
    healthcheck: Healthcheck
    mem_limit: str
    cpus: float
    pids_limit: int


class ComposeDocument(TypedDict):
    """A rendered compose file."""

    services: dict[str, ComposeService]
    networks: dict[str, dict[str, bool]]
    volumes: dict[str, dict[str, str]]
    secrets: dict[str, dict[str, str]]


class StackError(RuntimeError):
    """The episode's compose project could not deliver its logs."""


def render_compose(
    settings: LocArenaSettings,
    repository: Path,
    run: str,
    episode_arguments: list[str],
) -> ComposeDocument:
    """The compose file of one episode of ``run``; ``episode_arguments`` go to ``loc_arena.cli run``."""
    gateway, stack = settings.gateway, settings.stack
    configs = f"{repository / 'configs'}:{CONFIGS_DIRECTORY}:ro"
    probe = f"import socket; socket.create_connection(('localhost', {gateway.port}), 2)"
    shared: ComposeService = {"build": str(repository), "image": stack.image, "cap_drop": ["ALL"]}
    return {
        "services": {
            "gateway": {
                **shared,
                "command": ["python", "-m", "loc_arena.gateway.proxy", f"{CONFIGS_DIRECTORY}/{run}.yaml"],
                "secrets": [KEY_SECRET],
                "volumes": [f"sealed:{gateway.call_log.parent}", configs],
                "networks": [AGENT_NETWORK, EGRESS_NETWORK],
                "healthcheck": {"test": ["CMD", "python", "-c", probe], "interval": "2s", "retries": 15},
            },
            "episode": {
                **shared,
                "command": [
                    *["python", "-m", "loc_arena.cli", "run", "--run", run, *episode_arguments],
                    *["--out", str(OUTPUT_DIRECTORY)],
                ],
                "environment": {GATEWAY_URL_VARIABLE: f"http://gateway:{gateway.port}/{CHAT_COMPLETIONS}"},
                "volumes": [f"output:{OUTPUT_DIRECTORY}", configs],
                "networks": [AGENT_NETWORK],
                "depends_on": {"gateway": {"condition": "service_healthy"}},
                "mem_limit": stack.episode_memory_limit,
                "cpus": stack.episode_cpus,
                "pids_limit": stack.episode_pids_limit,
            },
        },
        "networks": {AGENT_NETWORK: {"internal": True}, EGRESS_NETWORK: {"internal": False}},
        "volumes": {"sealed": {}, "output": {}},
        "secrets": {KEY_SECRET: {"environment": API_KEY_VARIABLE}},
    }


def run_in_stack(run: str, episode_arguments: list[str], logs: Path, repository: Path) -> int:
    """Run one episode in its own compose project, copy its logs into ``logs``, and return its exit code."""
    project = f"locarena-{secrets.token_hex(3)}"
    settings = load_settings(repository / "configs" / f"{run}.yaml")
    compose_file = logs / "compose" / f"{project}.yaml"
    compose_file.parent.mkdir(parents=True, exist_ok=True)
    document = render_compose(settings, repository, run, episode_arguments)
    compose_file.write_text(yaml.safe_dump(document, sort_keys=False))
    compose = ["docker", "compose", "-p", project, "-f", str(compose_file)]

    # --exit-code-from stops the gateway when the episode exits
    up = [*compose, "up", "--build", "--attach", "episode", "--exit-code-from", "episode"]
    episode = subprocess.run(up)
    call_log = logs / "gateway" / f"{project}.calls.jsonl"
    call_log.parent.mkdir(parents=True, exist_ok=True)
    copies = [
        [*compose, "cp", f"episode:{OUTPUT_DIRECTORY}/.", str(logs)],
        [*compose, "cp", f"gateway:{settings.gateway.call_log}", str(call_log)],
    ]
    if any(subprocess.run(copy).returncode != 0 for copy in copies):
        subprocess.run([*compose, "stop"])
        raise StackError(f"could not copy the logs out: kept compose project {project} and its volumes")
    subprocess.run([*compose, "down", "--volumes", "--remove-orphans"])
    return episode.returncode
