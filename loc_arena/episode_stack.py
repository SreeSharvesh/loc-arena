"""One episode as a compose project: the gateway, and the episode in a container with no key and no internet.

The gateway is the only container with the provider key and a route out (agent-net and egress-net). The
episode container plays (``loc_arena.cli run --play-only``) on agent-net alone, so its model calls can only go
to the gateway. When it exits, its run directory is copied out and graded on this host, with the agents' code
sandboxed, while the gateway still runs for the monitors' model calls. Then the gateway's call log is copied
out and the project is removed with its volumes. If a copy fails, the project is kept so nothing is lost.
"""

from __future__ import annotations

import dataclasses
import secrets
import subprocess
from pathlib import Path, PurePosixPath
from typing import TypedDict
from urllib.parse import urlsplit

import yaml

from loc_arena.config import RunConfig, load_run_config
from loc_arena.gateway.core import API_KEY_VARIABLE, GATEWAY_URL_VARIABLE, OPENROUTER_URL, OpenRouterProvider
from loc_arena.harness import grade_run, locate_run
from loc_arena.settings import LocArenaSettings

AGENT_NETWORK = "agent-net"  # the episode and the gateway, with no route out
EGRESS_NETWORK = "egress-net"  # the gateway alone: its route to the provider
KEY_SECRET_NAME = "openrouter_api_key"  # the compose secret: a file in settings.gateway.secrets_dir
OUTPUT_DIRECTORY = PurePosixPath("/output")  # the episode's audit bundles; the image creates it for nobody
CONFIGS_DIRECTORY = PurePosixPath("/app/configs")  # the run configs, mounted read-only
REPOSITORY = Path(__file__).resolve().parents[1]  # its Dockerfile and configs/
LOOPBACK = "127.0.0.1"  # the only host address the gateway's port is published on, for grading on this host


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
    ports: list[str]
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
                "secrets": [KEY_SECRET_NAME],
                "volumes": [f"sealed:{gateway.call_log.parent}", configs],
                "networks": [AGENT_NETWORK, EGRESS_NETWORK],
                "ports": [f"{LOOPBACK}::{gateway.port}"],  # an ephemeral host port
                "healthcheck": {
                    "test": ["CMD", "python", "-c", probe],
                    "interval": f"{stack.gateway_health_interval_seconds}s",
                    "retries": stack.gateway_health_retries,
                },
            },
            "episode": {
                **shared,
                "command": [
                    *["python", "-m", "loc_arena.cli", "run", "--run", run, *episode_arguments],
                    *["--out", str(OUTPUT_DIRECTORY)],
                ],
                "environment": {
                    GATEWAY_URL_VARIABLE: f"http://gateway:{gateway.port}{urlsplit(OPENROUTER_URL).path}",
                },
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
        "secrets": {KEY_SECRET_NAME: {"environment": API_KEY_VARIABLE}},
    }


def run_in_stack(run: str, *, mode: str, seed: int | None, robust: bool, logs: Path) -> Path:
    """Play one episode of ``run`` (a config in ``configs/``) in its own compose project; grade it here.

    The episode container plays the episode and its honest twin. Its run directory is then copied into
    ``logs`` and graded on this host with the agents' code sandboxed, while the gateway still runs, so
    model-backed monitors reach their model through it and it records their calls. Whatever the episode and
    the gateway recorded is copied into ``logs`` before the project is removed, after a Ctrl-C too. When a
    copy fails the project is kept, stopped, so nothing recorded is lost. Returns the bundle's directory.
    """
    run = Path(run).name.removesuffix(".yaml")
    config = load_run_config(REPOSITORY / "configs" / f"{run}.yaml")
    settings = config.settings
    instance_id = secrets.token_hex(3)
    project = f"locarena-{instance_id}"
    episode_arguments = ["--mode", mode, "--instance", instance_id, "--play-only"]
    episode_arguments += [] if robust else ["--minimal"]
    compose_file = logs / "compose" / f"{project}.yaml"
    compose_file.parent.mkdir(parents=True, exist_ok=True)
    compose_file.write_text(yaml.safe_dump(render_compose(settings, REPOSITORY, run, episode_arguments)))
    # The repository is the project directory, so compose reads the key from its .env as `make run` does.
    compose = [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        str(compose_file),
        "--project-directory",
        str(REPOSITORY),
    ]
    call_log = logs / "gateway" / f"{project}.calls.jsonl"
    call_log.parent.mkdir(parents=True, exist_ok=True)
    copy_output = [*compose, "cp", f"episode:{OUTPUT_DIRECTORY}/.", str(logs)]
    copy_call_log = [*compose, "cp", f"gateway:{settings.gateway.call_log}", str(call_log)]
    created = output_copied = False
    try:
        if subprocess.run([*compose, "create", "--build"]).returncode != 0:
            raise StackError("could not build or create the episode's containers: see compose's output above")
        created = True
        # Started on its own, the gateway outlives the episode: the monitors call it while this host grades.
        if subprocess.run([*compose, "up", "--detach", "--wait", "gateway"]).returncode != 0:
            raise StackError("the gateway did not become healthy: see compose's output above")
        exit_code = subprocess.run(
            [*compose, "up", "--attach", "episode", "--exit-code-from", "episode", "episode"],
        ).returncode
        output_copied = subprocess.run(copy_output).returncode == 0
        if not output_copied:
            raise StackError("could not copy the episode's run directory out, so it was not graded")
        if exit_code != 0:
            raise StackError(f"the episode exited with code {exit_code}: see its output above")
        return grade_run(
            sandbox_agent_code(config),
            locate_run(config, mode, instance_id, logs),
            mode=mode,
            seed=seed,
            monitor_provider=_build_monitor_provider(config, compose),
        )
    finally:
        never_started = [*compose, "ps", "--all", "--status", "created", "--quiet", "episode"]
        started = created and not subprocess.run(never_started, capture_output=True, text=True).stdout.strip()
        pending = [copy_call_log] if output_copied else [copy_output, copy_call_log]
        if started and not all(subprocess.run(copy).returncode == 0 for copy in pending):
            subprocess.run([*compose, "stop"])
            raise StackError(f"could not copy every log out: kept compose project {project} and its volumes")
        subprocess.run([*compose, "down", "--volumes", "--remove-orphans"])
        if created and not started:
            message = f"the episode never started: is {API_KEY_VARIABLE} set in .env or the shell? See above"
            raise StackError(message)


def sandbox_agent_code(config: RunConfig) -> RunConfig:
    """``config`` with the agents' code run in a no-network container: always so when grading a stack run."""
    stack = config.settings.stack.model_copy(update={"sandbox_agent_code": True})
    return dataclasses.replace(config, settings=config.settings.model_copy(update={"stack": stack}))


def _build_monitor_provider(config: RunConfig, compose: list[str]) -> OpenRouterProvider | None:
    """The provider of a live run's monitors: the gateway, at the port it publishes on this host's loopback.

    A scripted run's monitors make no model call. A live run's never fall back to their heuristic for want of
    a provider: if the gateway's port cannot be found, grading stops.
    """
    if config.policy != "model":
        return None
    published = subprocess.run(
        [*compose, "port", "gateway", str(config.settings.gateway.port)],
        capture_output=True,
        text=True,
        check=True,
    )
    return OpenRouterProvider(gateway_url=f"http://{published.stdout.strip()}{urlsplit(OPENROUTER_URL).path}")
