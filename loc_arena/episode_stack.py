"""One episode as a compose project: the gateway, the episode and a sandbox per agent, no key, no internet.

The gateway is the only container with the provider key and a route out (agent-net and egress-net). The
episode container plays (``loc_arena.cli run --play-only``) on agent-net alone, so its model calls can only go
to the gateway. Each agent's code runs in its own sandbox, ``sandbox-<agent id>``, also on agent-net alone,
which mounts only the volume of the checkouts: never the episode's logs, the run configs or the gateway's call
log. Only the episode can call a sandbox's command server: a sandbox refuses every other caller, even one
holding its token. Code left in the shared checkout still runs wherever another agent runs it (the shared
checkout is a channel between agents by design). Each live service of the scenario runs in its own container
on agent-net alone, with no volume and no published port. On one with rights, every agent has its own
identity, generated per run: its sandbox holds that agent's alone, the service holds every agent's and the
agents' starting rights, and the episode and the gateway hold none. When the episode exits, its run directory
is copied out and graded on this host, with the agents' code sandboxed, while the gateway still runs for the
monitors' model calls. Then the gateway's call log and each live service's log, its record of every request
and grant, are copied out and the project is removed with its volumes. If a copy fails, the project is kept
so nothing is lost.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import json
import os
import secrets
import subprocess
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypedDict
from urllib.parse import urlsplit

import yaml
from scenarios.loader import LiveService

from loc_arena.config import RunConfig, load_run_config, sandbox_service
from loc_arena.gateway.core import API_KEY_VARIABLE, GATEWAY_URL_VARIABLE, OPENROUTER_URL, OpenRouterProvider
from loc_arena.harness import grade_run, locate_run
from loc_arena.sandbox import IDENTITY_PREFIX, TOKEN_FILE, build_server_settings, token_secret_name
from loc_arena.settings import StackSettings
from loc_arena.task import SANDBOX_URL_VARIABLE, resolve_scenario

EPISODE_SERVICE = "episode"  # the agent loop's compose service: the one caller every sandbox serves
AGENT_NETWORK = "agent-net"  # the episode, the sandboxes and the gateway, with no route out
EGRESS_NETWORK = "egress-net"  # the gateway alone: its route to the provider
KEY_SECRET_NAME = "openrouter_api_key"  # the compose secret: a file in settings.gateway.secrets_dir
OUTPUT_DIRECTORY = PurePosixPath("/output")  # the episode's audit bundles; the image creates it for nobody
CONFIGS_DIRECTORY = PurePosixPath("/app/configs")  # the run configs, mounted read-only
SCENARIOS_DIRECTORY = PurePosixPath("/app/scenarios")  # where the loader looks for a scenario pack
REPOSITORY = Path(__file__).resolve().parents[1]  # its Dockerfile and configs/
TOKEN_BYTES = 32  # the entropy of each sandbox token and of each agent's identity on a live service
VOCABULARY_VARIABLE = "LOC_ARENA_VOCABULARY"  # the rights a live service's scenario declares: a JSON list
RIGHTS_VARIABLE = "LOC_ARENA_RIGHTS"  # a live service's starting rights: JSON {agent id: [right, ...]}
TRANSITIVE_VARIABLE = "LOC_ARENA_TRANSITIVE"  # "true" when a live service lets the grant right be granted
GATEWAY_MODE_OPTION = "com.docker.network.bridge.gateway_mode_ipv4"
LOOPBACK = "127.0.0.1"  # the only host address the gateway's port is published on, for grading on this host
ENGINE_TARGET = "engine"  # the Dockerfile's stage of the gateway and the episode
SANDBOX_TARGET = "sandbox"  # the Dockerfile's stage of the sandboxes: no harness, no scenarios


class Healthcheck(TypedDict):
    """A compose healthcheck."""

    test: list[str]
    interval: str
    retries: int


class ComposeBuild(TypedDict):
    """A compose build: the Dockerfile's directory and the stage to build."""

    context: str
    target: str


class ServiceBuild(TypedDict):
    """A live service's compose build: the directory of its Dockerfile, built whole."""

    context: str


class ServiceSecret(TypedDict):
    """A compose secret mounted in a service under another file name: ``target`` in its secrets directory."""

    source: str
    target: str


class ComposeService(TypedDict, total=False):
    """The compose keys a rendered service uses."""

    build: ComposeBuild | ServiceBuild
    image: str
    pull_policy: str
    command: list[str]
    environment: dict[str, str]
    secrets: list[str | ServiceSecret]
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


@dataclass(frozen=True)
class Identity:
    """One agent's identity on one live service with rights: a compose secret, generated per run."""

    service: str
    agent_id: str

    @property
    def secret_name(self) -> str:
        """The compose secret: both names verbatim, which hold no ``_``, so no two identities share one."""
        return f"{IDENTITY_PREFIX}{self.service}_{self.agent_id}"

    @property
    def variable(self) -> str:
        """Where compose reads the identity from, on the host."""
        return f"LOC_ARENA_{self.secret_name.upper().replace('-', '_')}"


def issue_identities(config: RunConfig) -> tuple[Identity, ...]:
    """Every agent's identity on each live service with rights, agents with no rights there included.

    Refuses names that would make two identities share one host variable, and so one value: an agent could
    then pass as another.
    """
    identities = tuple(
        Identity(service.name, agent.id)
        for service in config.live_services
        if service.rights
        for agent in config.agents
    )
    variables = Counter(identity.variable for identity in identities)
    if shared := [identity for identity in identities if variables[identity.variable] > 1]:
        pairs = [(identity.service, identity.agent_id) for identity in shared]
        raise StackError(f"the identities of (service, agent) pairs {pairs} would share one host variable")
    return identities


def render_compose(
    config: RunConfig,
    repository: Path,
    run: str,
    episode_arguments: list[str],
) -> ComposeDocument:
    """The compose file of one episode of ``run``; ``episode_arguments`` go to ``loc_arena.cli run``.

    The images hold neither the scenario's sealed ``reference/`` nor its ``scripted/`` moves, so an agent with
    a shell cannot read the answer. Only a scripted episode, which plays them, gets ``scripted/`` mounted. The
    sandboxes' image holds no harness and no scenarios either, so agent code cannot read how it is graded.
    """
    gateway, stack = config.settings.gateway, config.settings.stack
    scripted = []
    if config.policy == "scripted":
        scenario = resolve_scenario(config)
        target = SCENARIOS_DIRECTORY / scenario.directory.name / scenario.scripted_dir.name
        scripted = [f"{scenario.scripted_dir}:{target}:ro"]
    configs = f"{repository / 'configs'}:{CONFIGS_DIRECTORY}:ro"
    checkouts = f"checkouts:{stack.checkouts_directory}"
    engine = _build_from(repository, ENGINE_TARGET, stack.image)
    agent_ids = [agent.id for agent in config.agents]
    services = config.live_services
    identities = issue_identities(config)
    return {
        "services": {
            "gateway": {
                **engine,
                "command": ["python", "-m", "loc_arena.gateway.proxy", f"{CONFIGS_DIRECTORY}/{run}.yaml"],
                "secrets": [KEY_SECRET_NAME],
                "volumes": [f"sealed:{gateway.call_log.parent}", configs],
                "networks": [AGENT_NETWORK, EGRESS_NETWORK],
                "ports": [f"{LOOPBACK}::{gateway.port}"],  # an ephemeral host port
                "healthcheck": _probe(gateway.port, stack),
            },
            **{
                sandbox_service(agent): _render_sandbox(
                    config,
                    repository,
                    agent,
                    identities,
                    builds=index == 0,
                )
                for index, agent in enumerate(agent_ids)
            },
            **{service.name: _render_live_service(config, service, identities) for service in services},
            EPISODE_SERVICE: {
                **engine,
                "mem_limit": stack.episode_memory_limit,
                "cpus": stack.episode_cpus,
                "pids_limit": stack.episode_pids_limit,
                "command": [
                    *["python", "-m", "loc_arena.cli", "run", "--run", run, *episode_arguments],
                    *["--out", str(OUTPUT_DIRECTORY)],
                ],
                "environment": {
                    GATEWAY_URL_VARIABLE: f"http://gateway:{gateway.port}{urlsplit(OPENROUTER_URL).path}",
                    SANDBOX_URL_VARIABLE: f"http://{sandbox_service('{agent}')}:{stack.sandbox_port}",
                },
                "secrets": [token_secret_name(agent_id) for agent_id in agent_ids],
                "volumes": [f"output:{OUTPUT_DIRECTORY}", checkouts, configs, *scripted],
                "networks": [AGENT_NETWORK],
                "depends_on": {
                    service: {"condition": "service_healthy"}
                    for service in [
                        "gateway",
                        *map(sandbox_service, agent_ids),
                        *(service.name for service in services),
                    ]
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
            **{token_secret_name(agent): {"environment": token_variable(agent)} for agent in agent_ids},
            **{identity.secret_name: {"environment": identity.variable} for identity in identities},
        },
    }


def _render_sandbox(
    config: RunConfig,
    repository: Path,
    agent_id: str,
    identities: tuple[Identity, ...],
    *,
    builds: bool,
) -> ComposeService:
    """The sandbox of ``agent_id``: its code's container, holding that agent's token and identities alone.

    One sandbox ``builds`` the image they all run, so compose builds it once; the others never pull it.
    """
    stack = config.settings.stack
    built = _build_from(repository, SANDBOX_TARGET, stack.sandbox_image)
    if not builds:
        del built["build"]
        built["pull_policy"] = "never"
    return {
        **built,
        "mem_limit": stack.sandbox_memory_limit,
        "cpus": stack.sandbox_cpus,
        "pids_limit": stack.sandbox_pids_limit,
        # It mounts no config, so it gets its settings in its command.
        "command": [
            *["python", "-m", "sandbox_server"],
            build_server_settings(config.settings, trusted_caller=EPISODE_SERVICE).model_dump_json(),
        ],
        # Its token under the one name every sandbox's command server reads; its identity on each service
        # under that service's name.
        "secrets": [
            {"source": token_secret_name(agent_id), "target": TOKEN_FILE},
            *(
                _mount(identity.secret_name, f"{IDENTITY_PREFIX}{identity.service}")
                for identity in identities
                if identity.agent_id == agent_id
            ),
        ],
        "volumes": [f"checkouts:{stack.checkouts_directory}"],
        "networks": [AGENT_NETWORK],
        "healthcheck": _probe(stack.sandbox_port, stack),
        "init": True,  # reaps the processes the agent's commands leave behind
        "restart": "unless-stopped",  # agent code can kill the server: it comes back
    }


def _render_live_service(
    config: RunConfig,
    service: LiveService,
    identities: tuple[Identity, ...],
) -> ComposeService:
    """A live service: on agent-net alone, no volume, no published port.

    One with rights holds every agent's identity, each under its agent's id, the rights its scenario declares
    and the agents' starting rights.
    """
    stack = config.settings.stack
    rights = {agent.id: list(agent.sandbox.rights.get(service.name, ())) for agent in config.agents}
    source: ComposeService = (
        {"build": {"context": str(service.source)}, "image": f"loc-arena-service-{service.name}:latest"}
        if isinstance(service.source, Path)
        else {"image": service.source}
    )
    return {
        **source,
        **({"command": list(service.command)} if service.command else {}),
        "cap_drop": ["ALL"],
        "mem_limit": stack.service_memory_limit,
        "cpus": stack.service_cpus,
        "pids_limit": stack.service_pids_limit,
        "networks": [AGENT_NETWORK],
        **(
            {
                "environment": {
                    VOCABULARY_VARIABLE: json.dumps(list(service.rights)),
                    RIGHTS_VARIABLE: json.dumps(rights),
                    TRANSITIVE_VARIABLE: json.dumps(service.transitive),
                },
                "secrets": [
                    _mount(identity.secret_name, f"{IDENTITY_PREFIX}{identity.agent_id}")
                    for identity in identities
                    if identity.service == service.name
                ],
            }
            if service.rights
            else {}
        ),
        "healthcheck": _probe(service.port, stack, test=service.healthcheck),
        "init": True,
    }


def _mount(secret: str, target: str) -> ServiceSecret:
    """``secret`` mounted as the file ``target`` in the service's secrets directory."""
    return {"source": secret, "target": target}


def token_variable(agent_id: str) -> str:
    """Where compose reads the token of ``agent_id``'s sandbox from, on the host."""
    return f"LOC_ARENA_{token_secret_name(agent_id).upper()}"


def _build_from(repository: Path, target: str, image: str) -> ComposeService:
    """A service with no capability, run as ``image``: the Dockerfile stage ``target`` in ``repository``."""
    return {"build": {"context": str(repository), "target": target}, "image": image, "cap_drop": ["ALL"]}


def _probe(port: int, stack: StackSettings, *, test: Sequence[str] | None = None) -> Healthcheck:
    """A healthcheck running ``test``; by default it passes once something accepts connections on ``port``."""
    probe = ["CMD", "python", "-c", f"import socket; socket.create_connection(('localhost', {port}), 2)"]
    return {
        "test": list(test or probe),
        "interval": f"{stack.gateway_health_interval_seconds}s",
        "retries": stack.gateway_health_retries,
    }


def run_in_stack(run: str, *, mode: str, seed: int | None, robust: bool, logs: Path) -> Path:
    """Play one episode of ``run`` (a config in ``configs/``) in its own compose project; grade it here.

    The episode container plays the episode and its honest twin. Its run directory is then copied into
    ``logs`` and graded on this host with the agents' code sandboxed, while the gateway still runs, so
    model-backed monitors reach their model through it and it records their calls. Whatever the episode, the
    gateway and each live service recorded is copied into ``logs`` before the project is removed, after a
    Ctrl-C too, a live service's log to ``services/<name>.log``. When a copy fails the project is kept,
    stopped, so nothing recorded is lost. Returns the bundle's directory.
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
    # Each sandbox's token and each agent's identity on each live service reach compose through its
    # environment alone: never a command line or a file.
    tokens = {token_variable(agent.id): secrets.token_urlsafe(TOKEN_BYTES) for agent in config.agents}
    identities = {
        identity.variable: secrets.token_urlsafe(TOKEN_BYTES) for identity in issue_identities(config)
    }
    run_compose = functools.partial(subprocess.run, env={**os.environ, **tokens, **identities})

    def copied(command: list[str], into: Path | None = None) -> bool:
        """Whether compose ran ``command``, with what it prints written ``into`` that file when given."""
        with into.open("wb") if into else contextlib.nullcontext() as output:
            return run_compose(command, stdout=output).returncode == 0

    call_log = logs / "gateway" / f"{project}.calls.jsonl"
    service_logs = logs / "services"
    for directory in (call_log.parent, service_logs):
        directory.mkdir(parents=True, exist_ok=True)
    copy_output = [*compose, "cp", f"{EPISODE_SERVICE}:{OUTPUT_DIRECTORY}/.", str(logs)]
    copy_logs: list[tuple[list[str], Path | None]] = [
        ([*compose, "cp", f"gateway:{settings.gateway.call_log}", str(call_log)], None),
        *(
            (
                [*compose, "logs", "--no-color", "--no-log-prefix", service.name],
                service_logs / f"{service.name}.log",
            )
            for service in config.live_services
        ),
    ]
    created = output_copied = False
    try:
        if run_compose([*compose, "create", "--build"]).returncode != 0:
            raise StackError("could not build or create the episode's containers: see compose's output above")
        created = True
        # Started on its own, the gateway outlives the episode: the monitors call it while this host grades.
        if run_compose([*compose, "up", "--detach", "--wait", "gateway"]).returncode != 0:
            raise StackError("the gateway did not become healthy: see compose's output above")
        play = ["up", "--attach", EPISODE_SERVICE, "--exit-code-from", EPISODE_SERVICE, EPISODE_SERVICE]
        exit_code = run_compose([*compose, *play]).returncode
        output_copied = copied(copy_output)
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
        never_started = [*compose, "ps", "--all", "--status", "created", "--quiet", EPISODE_SERVICE]
        started = created and not run_compose(never_started, capture_output=True, text=True).stdout.strip()
        pending = copy_logs if output_copied else [(copy_output, None), *copy_logs]
        # Every copy is tried, a failed one included, before the project is kept.
        if started and not all([copied(*copy) for copy in pending]):
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
    return OpenRouterProvider(
        timeout=config.settings.gateway.timeout_seconds,
        gateway_url=f"http://{published.stdout.strip()}{urlsplit(OPENROUTER_URL).path}",
        model_providers=config.model_providers,
    )
