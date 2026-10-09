"""One episode as a compose project: the gateway, the episode and its sandbox, with no key and no internet.

The gateway is the only container with the provider key and a route out (agent-net and egress-net). The
episode container plays (``loc_arena.cli run --play-only``) on agent-net alone, so its model calls can only go
to the gateway. The agents' code runs in the sandbox, also on agent-net alone, which mounts only the volume of
the checkouts: never the episode's logs, the run configs or the gateway's call log. When the episode exits,
its run directory is copied out and graded on this host, with the agents' code sandboxed, while the gateway
still runs for the monitors' model calls. Then the gateway's call log is copied out and the project is
removed with its volumes. If a copy fails, the project is kept so nothing is lost.
"""

from __future__ import annotations

import dataclasses
import functools
import os
import secrets
import subprocess
from pathlib import Path, PurePosixPath
from typing import TypedDict
from urllib.parse import urlsplit

import yaml

from loc_arena.config import RunConfig, load_run_config
from loc_arena.gateway.core import API_KEY_VARIABLE, GATEWAY_URL_VARIABLE, OPENROUTER_URL, OpenRouterProvider
from loc_arena.harness import grade_run, locate_run
from loc_arena.sandbox import SANDBOX_SERVICE, TOKEN_SECRET_NAME, TOKEN_VARIABLE
from loc_arena.settings import StackSettings
from loc_arena.task import SANDBOX_URL_VARIABLE, resolve_scenario

AGENT_NETWORK = "agent-net"  # the episode, the sandbox and the gateway, with no route out
EGRESS_NETWORK = "egress-net"  # the gateway alone: its route to the provider
KEY_SECRET_NAME = "openrouter_api_key"  # the compose secret: a file in settings.gateway.secrets_dir
OUTPUT_DIRECTORY = PurePosixPath("/output")  # the episode's audit bundles; the image creates it for nobody
CONFIGS_DIRECTORY = PurePosixPath("/app/configs")  # the run configs, mounted read-only
SCENARIOS_DIRECTORY = PurePosixPath("/app/scenarios")  # where the loader looks for a scenario pack
REPOSITORY = Path(__file__).resolve().parents[1]  # its Dockerfile and configs/
TOKEN_BYTES = 32  # the sandbox token's entropy
GATEWAY_MODE_OPTION = "com.docker.network.bridge.gateway_mode_ipv4"
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
    init: bool
    restart: str
    depends_on: dict[str, dict[str, str]]
    healthcheck: Healthcheck
    mem_limit: str
    cpus: float
    pids_limit: int


class ComposeNetwork(TypedDict, total=False):
    """The compose keys a rendered network uses."""

    internal: bool
    driver_opts: dict[str, str]


class ComposeDocument(TypedDict):
    """A rendered compose file."""

    services: dict[str, ComposeService]
    networks: dict[str, ComposeNetwork]
    volumes: dict[str, dict[str, str]]
    secrets: dict[str, dict[str, str]]


class StackError(RuntimeError):
    """The episode's compose project could not deliver its logs."""


def render_compose(
    config: RunConfig,
    repository: Path,
    run: str,
    episode_arguments: list[str],
) -> ComposeDocument:
    """The compose file of one episode of ``run``; ``episode_arguments`` go to ``loc_arena.cli run``.

    The image holds neither the scenario's sealed ``reference/`` nor its ``scripted/`` moves, so an agent with
    a shell cannot read the answer. Only a scripted episode, which plays them, gets ``scripted/`` mounted.
    """
    gateway, stack = config.settings.gateway, config.settings.stack
    scripted = []
    if config.policy == "scripted":
        scenario = resolve_scenario(config)
        target = SCENARIOS_DIRECTORY / scenario.directory.name / scenario.scripted_dir.name
        scripted = [f"{scenario.scripted_dir}:{target}:ro"]
    configs = f"{repository / 'configs'}:{CONFIGS_DIRECTORY}:ro"
    checkouts = f"checkouts:{stack.checkouts_directory}"
    shared: ComposeService = {"build": str(repository), "image": stack.image, "cap_drop": ["ALL"]}
    limits: ComposeService = {
        "mem_limit": stack.episode_memory_limit,
        "cpus": stack.episode_cpus,
        "pids_limit": stack.episode_pids_limit,
    }
    return {
        "services": {
            "gateway": {
                **shared,
                "command": ["python", "-m", "loc_arena.gateway.proxy", f"{CONFIGS_DIRECTORY}/{run}.yaml"],
                "secrets": [KEY_SECRET_NAME],
                "volumes": [f"sealed:{gateway.call_log.parent}", configs],
                "networks": [AGENT_NETWORK, EGRESS_NETWORK],
                "ports": [f"{LOOPBACK}::{gateway.port}"],  # an ephemeral host port
                "healthcheck": _probe(gateway.port, stack),
            },
            SANDBOX_SERVICE: {
                **shared,
                **limits,
                # It mounts no config, so it gets the run's settings in its command.
                "command": ["python", "-m", "loc_arena.sandbox", config.settings.model_dump_json()],
                "secrets": [TOKEN_SECRET_NAME],
                "volumes": [checkouts],
                "networks": [AGENT_NETWORK],
                "healthcheck": _probe(stack.sandbox_port, stack),
                "init": True,  # reaps the processes the agents' commands leave behind
                "restart": "unless-stopped",  # agent code can kill the server: it comes back
            },
            "episode": {
                **shared,
                **limits,
                "command": [
                    *["python", "-m", "loc_arena.cli", "run", "--run", run, *episode_arguments],
                    *["--out", str(OUTPUT_DIRECTORY)],
                ],
                "environment": {
                    GATEWAY_URL_VARIABLE: f"http://gateway:{gateway.port}{urlsplit(OPENROUTER_URL).path}",
                    SANDBOX_URL_VARIABLE: f"http://{SANDBOX_SERVICE}:{stack.sandbox_port}",
                },
                "secrets": [TOKEN_SECRET_NAME],
                "volumes": [f"output:{OUTPUT_DIRECTORY}", checkouts, configs, *scripted],
                "networks": [AGENT_NETWORK],
                "depends_on": {
                    "gateway": {"condition": "service_healthy"},
                    SANDBOX_SERVICE: {"condition": "service_healthy"},
                },
            },
        },
        "networks": {
            # Isolated: the host takes no address on agent-net, so agent code cannot reach its services there
            # (Docker Engine 28 or later).
            AGENT_NETWORK: {"internal": True, "driver_opts": {GATEWAY_MODE_OPTION: "isolated"}},
            EGRESS_NETWORK: {"internal": False},
        },
        "volumes": {"sealed": {}, "output": {}, "checkouts": {}},
        "secrets": {
            KEY_SECRET_NAME: {"environment": API_KEY_VARIABLE},
            TOKEN_SECRET_NAME: {"environment": TOKEN_VARIABLE},
        },
    }


def _probe(port: int, stack: StackSettings) -> Healthcheck:
    """A healthcheck that passes once something in the container accepts connections on ``port``."""
    return {
        "test": ["CMD", "python", "-c", f"import socket; socket.create_connection(('localhost', {port}), 2)"],
        "interval": f"{stack.gateway_health_interval_seconds}s",
        "retries": stack.gateway_health_retries,
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
    compose_file.write_text(yaml.safe_dump(render_compose(config, REPOSITORY, run, episode_arguments)))
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
    # The sandbox token reaches compose through its environment alone: never a command line or a file.
    run_compose = functools.partial(
        subprocess.run,
        env={**os.environ, TOKEN_VARIABLE: secrets.token_urlsafe(TOKEN_BYTES)},
    )
    call_log = logs / "gateway" / f"{project}.calls.jsonl"
    call_log.parent.mkdir(parents=True, exist_ok=True)
    copy_output = [*compose, "cp", f"episode:{OUTPUT_DIRECTORY}/.", str(logs)]
    copy_call_log = [*compose, "cp", f"gateway:{settings.gateway.call_log}", str(call_log)]
    created = output_copied = False
    try:
        if run_compose([*compose, "create", "--build"]).returncode != 0:
            raise StackError("could not build or create the episode's containers: see compose's output above")
        created = True
        # Started on its own, the gateway outlives the episode: the monitors call it while this host grades.
        if run_compose([*compose, "up", "--detach", "--wait", "gateway"]).returncode != 0:
            raise StackError("the gateway did not become healthy: see compose's output above")
        exit_code = run_compose(
            [*compose, "up", "--attach", "episode", "--exit-code-from", "episode", "episode"],
        ).returncode
        output_copied = run_compose(copy_output).returncode == 0
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
        started = created and not run_compose(never_started, capture_output=True, text=True).stdout.strip()
        pending = [copy_call_log] if output_copied else [copy_output, copy_call_log]
        if started and not all(run_compose(copy).returncode == 0 for copy in pending):
            run_compose([*compose, "stop"])
            raise StackError(f"could not copy every log out: kept compose project {project} and its volumes")
        run_compose([*compose, "down", "--volumes", "--remove-orphans"])
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
