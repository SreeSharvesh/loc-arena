"""One episode as a compose project: the gateway, the episode and a sandbox per agent, no key, no internet.

The gateway is the only container with the provider key and a route out (agent-net and egress-net). The
episode container plays (``loc_arena.cli run --play-only``) on agent-net alone, so its model calls can only go
to the gateway. Each agent's code runs in its own sandbox, ``sandbox-<agent id>``, also on agent-net alone,
which mounts only the volume of the checkouts: never the episode's logs, the run configs or the gateway's call
log. Only the episode can call a sandbox's command server: a sandbox refuses every other caller, even one
holding its token. Code left in the shared checkout still runs wherever another agent runs it (the shared
checkout is a channel between agents by design). Each live service of the scenario runs in its own container
on agent-net alone, with no published port, and one may hand work to runners (``loc_arena.runner_pool``). On
one with rights, every agent has its own identity, generated per run: its sandbox holds that agent's alone,
the service holds every agent's and the agents' starting rights, and the episode and the gateway hold none. A
service with tools is reached through the tools gateway alone (``loc_arena.tools_gateway``), which with the
service holds the identities on it. In attack mode the episode container plays the episode, then starts again
for the honest twin once the gateway and every sandbox, runner, live service and the tools gateway is
recreated, so the twin sees nothing the attack left. When the episode exits, its run
directory is copied out with the log of each live service and of the tools gateway, their record of every
request, grant and tool call, the events of those tool calls are built, and the run is graded on this host,
with the agents' code sandboxed, while the gateway still runs for the monitors' model calls. Then the
gateway's record is drained and copied out (``loc_arena.gateway.model_gateway``) and the project is removed
with its volumes. If a copy fails, the project is kept so nothing is lost.
"""

from __future__ import annotations

import dataclasses
import functools
import os
import secrets
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from scenarios.loader import EngineModule, LiveService

from loc_arena.compose_document import ComposeDocument, ComposeService, Healthcheck, ServiceSecret
from loc_arena.config import RunConfig, load_run_config, sandbox_service
from loc_arena.gateway import model_gateway
from loc_arena.gateway.core import API_KEY_VARIABLE, OPENROUTER_URL, OpenRouterProvider
from loc_arena.harness import apply_mode, build_recorded_run_events, grade_run, locate_run
from loc_arena.iam_bootstrap import (
    BOOTSTRAP_SECRET,
    BOOTSTRAP_VARIABLE,
    CREDENTIAL_FILE,
    INBOX_KEY_PREFIX,
    SERVICE_CREDENTIAL_PREFIX,
    inbox_key_variable,
    managed_services,
    manifest_path,
    render_bootstrap,
    service_credential_variable,
)
from loc_arena.iam_wiring import service_environment
from loc_arena.identity_variables import find_shared_identity_variable, holds_identities, identity_variable
from loc_arena.node_pool import (
    NODE_TARGET,
    declare_node_token_secrets,
    list_node_services,
    node_token_secret,
    node_token_variable,
    render_nodes,
)
from loc_arena.observability import SpansNotExportedError
from loc_arena.recorded_events import SERVICE_LOGS
from loc_arena.runner_pool import (
    RUNNER_TARGET,
    declare_runner_token_secrets,
    list_runner_services,
    render_runners,
    runner_token_secret,
    runner_token_variable,
)
from loc_arena.sandbox import IDENTITY_PREFIX, TOKEN_FILE, build_server_settings, token_secret_name
from loc_arena.settings import StackSettings
from loc_arena.stack_play import (
    EPISODE_SERVICE,
    StackError,
    copy_out,
    list_service_log_copies,
    play_phases,
)
from loc_arena.task import SANDBOX_URL_VARIABLE, TOOLS_URL_VARIABLE, resolve_scenario
from loc_arena.tasks.main_task_grader import COMPANY_REPOS
from loc_arena.tools_gateway import (
    MCP_PATH,
    TOOLS_GATEWAY_CONFIG_SECRET_NAME,
    TOOLS_GATEWAY_CONFIG_VARIABLE,
    TOOLS_GATEWAY_SERVICE,
    render_tools_gateway_config,
    serves_tools,
)
from loc_arena.yaml_documents import dump_document

AGENT_NETWORK = "agent-net"  # the episode, the sandboxes and the gateway, with no route out
EGRESS_NETWORK = "egress-net"  # the gateway alone: its route to the provider
OUTPUT_DIRECTORY = PurePosixPath("/output")  # the episode's audit bundles; the image creates it for nobody
CONFIGS_DIRECTORY = PurePosixPath("/app/configs")  # the run configs, mounted read-only
SCENARIOS_DIRECTORY = PurePosixPath("/app/scenarios")  # where the loader looks for a scenario pack
COMPANY_DIRECTORY = PurePosixPath("/app/company")  # the company repos, in the engine image
REPOSITORY = Path(__file__).resolve().parents[1]  # its Dockerfile and configs/
PLATFORM_PACKAGE = "meridian-iam"  # on every engine service's path, never in the agents' checkout
IAM_MANIFEST_DIRECTORY = PurePosixPath("/srv")  # where a built managed service finds its own manifest
ENGINE_DIRECTORY = PurePosixPath("/app")  # the repository, in the engine image
TOKEN_BYTES = 32  # the entropy of each sandbox token and of each agent's identity on a live service
NEVER_STARTED = (
    f"the episode never started: is {API_KEY_VARIABLE} set in .env or the shell, or did a live service fail "
    "to become healthy? See above and the copied logs in {logs}"
)
GATEWAY_MODE_OPTION = "com.docker.network.bridge.gateway_mode_ipv4"
ENGINE_TARGET = "engine"  # the Dockerfile's stage of the gateway and the episode
SANDBOX_TARGET = "sandbox"  # the Dockerfile's stage of the sandboxes: no harness, no scenarios


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
        return identity_variable(self.service, self.agent_id)


def issue_identities(config: RunConfig) -> tuple[Identity, ...]:
    """Every agent's identity on each live service with rights or tools, agents with none there included.

    Refuses names that would make two identities share one host variable, as the config load does.
    """
    if shared := find_shared_identity_variable((agent.id for agent in config.agents), config.live_services):
        raise StackError(shared)
    return tuple(
        Identity(service.name, agent.id)
        for service in config.live_services
        if holds_identities(service)
        for agent in config.agents
    )


def render_compose(
    config: RunConfig,
    repository: Path,
    run: str,
    episode_arguments: list[str],
) -> ComposeDocument:
    """The compose file of one episode of ``run``; ``episode_arguments`` go to ``loc_arena.cli run``.

    The images hold neither the scenario's sealed ``reference/`` nor its ``scripted/`` moves, so an agent with
    a shell cannot read the answer. Only a scripted episode, which plays them, gets ``scripted/`` mounted. The
    sandboxes' image holds no harness and no scenarios either, so agent code cannot read how it is graded;
    each sandbox mounts only the scenario's skills folder, read-only. Every service an agent reaches runs
    under central IAM, so a run config that turns it off cannot run in a stack.
    """
    if not config.settings.iam.enabled:
        raise StackError("a stack run needs central IAM: this run config turns iam.enabled off")
    stack = config.settings.stack
    scenario = resolve_scenario(config)
    skills = [f"{scenario.skills_dir}:{stack.skills_directory}:ro"] if scenario.skills_dir else []
    scripted = []
    if config.policy == "scripted":
        target = SCENARIOS_DIRECTORY / scenario.directory.name / scenario.scripted_dir.name
        scripted = [f"{scenario.scripted_dir}:{target}:ro"]
    configs = f"{repository / 'configs'}:{CONFIGS_DIRECTORY}:ro"
    checkouts = f"checkouts:{stack.checkouts_directory}"
    engine = _build_from(repository, ENGINE_TARGET, stack.image)
    agent_ids = [agent.id for agent in config.agents]
    services = config.live_services
    tool_services = [service.name for service in services if service.tools]
    # A service with rights too (IAM) is reached from the sandboxes as well, so they keep its identity.
    tools_only = [service.name for service in services if service.tools and not service.rights]
    identities = issue_identities(config)
    # An engine module may serve company code: each company repo's root is on its path, as in a checkout, and
    # so is meridian-iam, a platform package rather than a work repo, for the services IAM manages.
    managed = managed_services(config)
    repos = (*COMPANY_REPOS, PLATFORM_PACKAGE)
    company_repos_pythonpath = ":".join(str(COMPANY_DIRECTORY / repo) for repo in repos)
    return {
        "services": {
            **model_gateway.render_gateway_services(config.settings, AGENT_NETWORK, EGRESS_NETWORK),
            **{
                sandbox_service(agent): _render_sandbox(
                    config,
                    repository,
                    agent,
                    [identity for identity in identities if identity.service not in tools_only],
                    skills,
                    builds=index == 0,
                )
                for index, agent in enumerate(agent_ids)
            },
            **{
                service.name: _render_live_service(
                    config,
                    service,
                    identities,
                    {**engine, "volumes": [configs], "environment": {"PYTHONPATH": company_repos_pythonpath}},
                    run,
                    managed,
                )
                for service in services
            },
            **render_runners(
                config,
                _build_from(repository, RUNNER_TARGET, stack.runner_image),
                _probe(stack.sandbox_port, stack),
                AGENT_NETWORK,
            ),
            **render_nodes(
                config,
                _build_from(repository, NODE_TARGET, stack.worker_image),
                _probe(stack.sandbox_port, stack),
                AGENT_NETWORK,
                skills,
            ),
            **(
                {TOOLS_GATEWAY_SERVICE: _render_tools_gateway(config, tool_services)} if tool_services else {}
            ),
            EPISODE_SERVICE: _render_episode(
                config,
                engine,
                run,
                episode_arguments,
                [f"output:{OUTPUT_DIRECTORY}", checkouts, configs, *scripted],
            ),
        },
        "networks": {
            # Isolated: the host takes no address on agent-net, so agent code cannot reach its services there
            # (Docker Engine 28 or later).
            AGENT_NETWORK: {"internal": True, "driver_opts": {GATEWAY_MODE_OPTION: "isolated"}},
            EGRESS_NETWORK: {"internal": False},
            model_gateway.RECORD_NETWORK: {"internal": True},
        },
        "volumes": {"sealed": {}, "output": {}, "checkouts": {}},
        "secrets": {
            **model_gateway.declare_gateway_secrets(),
            **{token_secret_name(agent): {"environment": token_variable(agent)} for agent in agent_ids},
            **{identity.secret_name: {"environment": identity.variable} for identity in identities},
            **declare_runner_token_secrets(config),
            **declare_node_token_secrets(config),
            **(
                {
                    BOOTSTRAP_SECRET: {"environment": BOOTSTRAP_VARIABLE},
                    **{
                        f"{SERVICE_CREDENTIAL_PREFIX}{name}": {
                            "environment": service_credential_variable(name),
                        }
                        for name in managed
                    },
                    **{
                        f"{INBOX_KEY_PREFIX}{agent}": {"environment": inbox_key_variable(agent)}
                        for agent in agent_ids
                    },
                }
                if managed  # a run with no service for IAM to manage issues none of its secrets
                else {}
            ),
            **(
                {TOOLS_GATEWAY_CONFIG_SECRET_NAME: {"environment": TOOLS_GATEWAY_CONFIG_VARIABLE}}
                if tool_services
                else {}
            ),
        },
    }


def _render_episode(
    config: RunConfig,
    engine: ComposeService,
    run: str,
    episode_arguments: list[str],
    volumes: list[str],
) -> ComposeService:
    """The episode: plays ``run`` once every other service is up, with every sandbox token and no identity."""
    stack = config.settings.stack
    agent_ids = [agent.id for agent in config.agents]
    tools_url = (
        f"http://{TOOLS_GATEWAY_SERVICE}:{stack.tools_gateway_port}{MCP_PATH}"
        if serves_tools(config)
        else None
    )
    return {
        **engine,
        "mem_limit": stack.episode_memory_limit,
        "cpus": stack.episode_cpus,
        "pids_limit": stack.episode_pids_limit,
        "command": [
            *["python", "-m", "loc_arena.cli", "run", "--run", run, *episode_arguments],
            *["--out", str(OUTPUT_DIRECTORY)],
        ],
        "environment": {
            **model_gateway.render_episode_environment(config.settings),
            SANDBOX_URL_VARIABLE: f"http://{sandbox_service('{agent}')}:{stack.sandbox_port}",
            **({TOOLS_URL_VARIABLE: tools_url} if tools_url else {}),
        },
        "secrets": [
            model_gateway.GATEWAY_TOKEN_SECRET_NAME,
            *(token_secret_name(agent_id) for agent_id in agent_ids),
        ],
        "volumes": volumes,
        "networks": [AGENT_NETWORK],
        "depends_on": {
            **{
                service: {"condition": "service_healthy"}
                for service in [
                    *map(sandbox_service, agent_ids),
                    *(service.name for service in config.live_services),
                ]
            },
            # No shell to probe: the host waits for the gateway, and MCP clients retry the tools gateway.
            "gateway": {"condition": "service_started"},
            **({TOOLS_GATEWAY_SERVICE: {"condition": "service_started"}} if tools_url else {}),
        },
    }


def _render_sandbox(
    config: RunConfig,
    repository: Path,
    agent_id: str,
    identities: Sequence[Identity],
    skills: list[str],
    *,
    builds: bool,
) -> ComposeService:
    """The sandbox of ``agent_id``: its code's container, holding that agent's token and ``identities`` alone.

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
        "volumes": [f"checkouts:{stack.checkouts_directory}", *skills],
        "networks": [AGENT_NETWORK],
        "healthcheck": _probe(stack.sandbox_port, stack),
        "init": True,  # reaps the processes the agent's commands leave behind
        "restart": "unless-stopped",  # agent code can kill the server: it comes back
    }


def _render_live_service(  # noqa: PLR0913
    config: RunConfig,
    service: LiveService,
    identities: tuple[Identity, ...],
    engine: ComposeService,
    run: str,
    managed: Sequence[str] = (),
) -> ComposeService:
    """A live service: on agent-net alone, no published port.

    One that runs a module of the ``engine`` service, with the run configs mounted read-only and the company
    repos importable, is given the path of ``run``'s config; any other mounts no volume.

    Central IAM decides for every service ``managed`` names: such a service holds no agent identity, only its
    own IAM credential, and waits for IAM; one built from its pack is built with the IAM client as a named
    context. IAM holds every identity (to know their digests), each service's credential and the play's
    bootstrap document, and keeps its store in an anonymous volume.
    """
    stack = config.settings.stack
    iam = config.settings.iam
    is_iam, is_managed = bool(managed) and service.name == iam.service, service.name in managed
    if holds_identities(service) and not (is_iam or is_managed):
        raise StackError(f"agents reach service {service.name!r}, but it is not under central IAM")
    environment: dict[str, str] = {}
    match service.source:
        case Path():
            source: ComposeService = {
                "build": {"context": str(service.source)},
                "image": f"loc-arena-service-{service.name}:latest",
            }
            manifest_in_container = None
            if is_managed:
                if PLATFORM_PACKAGE not in (service.source / "Dockerfile").read_text():
                    raise StackError(
                        f"service {service.name!r} is under IAM, but its Dockerfile copies in no "
                        f"meridian_iam from the {PLATFORM_PACKAGE!r} build context",
                    )
                manifest = Path(manifest_path(config, service.name))
                manifest_in_container = IAM_MANIFEST_DIRECTORY / manifest.name
                source["build"] = {
                    "context": str(service.source),
                    "additional_contexts": {PLATFORM_PACKAGE: str(REPOSITORY / "company" / PLATFORM_PACKAGE)},
                }
        case EngineModule(name=module):
            # The entry name follows the config so one module (the recording service) can serve several.
            arguments = [f"{CONFIGS_DIRECTORY}/{run}.yaml", service.name]
            source = {**engine, "command": ["python", "-m", module, *arguments]}
            environment = engine["environment"]
            manifest_in_container = (
                ENGINE_DIRECTORY / manifest_path(config, service.name) if is_managed else None
            )
        case image:
            if is_managed:
                raise StackError(f"service {service.name!r} runs a ready image, which cannot join IAM")
            source = {"image": image}
    secrets_directory = config.settings.gateway.secrets_dir
    if is_managed:
        credential = str(secrets_directory / CREDENTIAL_FILE)
        environment = {**environment, **service_environment(iam, str(manifest_in_container), credential)}
    mounted: list[str | ServiceSecret] = []
    if is_iam:
        mounted = [
            *(identity.secret_name for identity in identities),
            BOOTSTRAP_SECRET,
            *(f"{SERVICE_CREDENTIAL_PREFIX}{name}" for name in managed),
            *(f"{INBOX_KEY_PREFIX}{agent.id}" for agent in config.agents),
        ]
    elif is_managed:
        mounted = [_mount(f"{SERVICE_CREDENTIAL_PREFIX}{service.name}", CREDENTIAL_FILE)]
    runners = list_runner_services(config) if service.runners else []
    nodes = list_node_services(config) if service.nodes else []
    mounted = [*mounted, *map(runner_token_secret, runners), *map(node_token_secret, nodes)]
    depends_on = {
        **({iam.service: {"condition": "service_healthy"}} if is_managed else {}),
        **{runner: {"condition": "service_healthy"} for runner in runners},
        **{node: {"condition": "service_healthy"} for node in nodes},
    }
    return {
        **source,
        **({"command": list(service.command)} if service.command else {}),
        "cap_drop": ["ALL"],
        "mem_limit": stack.service_memory_limit,
        "cpus": stack.service_cpus,
        "pids_limit": stack.service_pids_limit,
        "networks": [AGENT_NETWORK],
        **({"environment": environment} if environment else {}),
        **({"secrets": mounted} if mounted else {}),
        **({"volumes": [*source.get("volumes", []), str(iam.database.parent)]} if is_iam else {}),
        **({"depends_on": depends_on} if depends_on else {}),
        "healthcheck": _probe(service.port, stack, test=service.healthcheck),
        "init": True,
    }


def _render_tools_gateway(config: RunConfig, tool_services: Sequence[str]) -> ComposeService:
    """The tools gateway: its pinned image, on agent-net alone, bounded as a live service, config a secret.

    It starts once every service it puts on its route is healthy.
    """
    stack = config.settings.stack
    return {
        "image": stack.tools_gateway_image,
        "command": ["-f", str(config.settings.gateway.secrets_dir / TOOLS_GATEWAY_CONFIG_SECRET_NAME)],
        "secrets": [TOOLS_GATEWAY_CONFIG_SECRET_NAME],
        "cap_drop": ["ALL"],
        "mem_limit": stack.service_memory_limit,
        "cpus": stack.service_cpus,
        "pids_limit": stack.service_pids_limit,
        "networks": [AGENT_NETWORK],
        "depends_on": {service: {"condition": "service_healthy"} for service in tool_services},
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
        "start_period": f"{stack.gateway_health_start_seconds}s",
        "start_interval": f"{stack.gateway_health_interval_seconds}s",
    }


def issue_secrets(config: RunConfig) -> tuple[dict[str, str], dict[str, str]]:
    """Every secret of a stack run, by the host variable compose reads: the attack env and the twin's."""
    tokens = {agent.id: secrets.token_urlsafe(TOKEN_BYTES) for agent in config.agents}
    held = model_gateway.issue_held_tokens(TOKEN_BYTES)
    sandboxes = {sandbox_service(agent): token for agent, token in tokens.items()}

    def with_gateway(environment: dict[str, str], play: model_gateway.Play) -> dict[str, str]:
        compute = {
            **{name: environment[runner_token_variable(name)] for name in list_runner_services(config)},
            **{name: environment[node_token_variable(name)] for name in list_node_services(config)},
        }
        gateway_secrets = model_gateway.issue_gateway_secrets(config.settings, held, sandboxes, compute, play)
        return environment | gateway_secrets

    attack, twin = _issue_play_secrets(config, tokens)
    return with_gateway(attack, "attack"), with_gateway(twin, "twin")


def _issue_play_secrets(config: RunConfig, tokens: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Each play's secrets but the model gateway's, with these sandbox ``tokens``: attack env and twin's.

    They reach compose through its environment alone: never a command line or a file. The honest twin's env
    differs in what the recreated containers are given. Its tools gateway offers the honest config's tools (no
    covert tool), so an agent granted one in the attack config is refused it there after ``renew_services``
    recreates the gateway. It also differs in the bootstrap document (rendered from the honest config, so no
    covert tool carries a grant) and in every identity and IAM credential: the sandboxes and services are
    recreated with new ones, so nothing the attack play held authenticates in the twin.
    """
    managed = managed_services(config)
    runners = list_runner_services(config)
    nodes = list_node_services(config)

    def issue_for_play(for_config: RunConfig) -> dict[str, str]:
        identities = {identity: secrets.token_urlsafe(TOKEN_BYTES) for identity in issue_identities(config)}
        environment = {
            **{token_variable(agent): token for agent, token in tokens.items()},
            **{identity.variable: value for identity, value in identities.items()},
            **{runner_token_variable(runner): secrets.token_urlsafe(TOKEN_BYTES) for runner in runners},
            **{node_token_variable(node): secrets.token_urlsafe(TOKEN_BYTES) for node in nodes},
        }
        if managed:
            environment[BOOTSTRAP_VARIABLE] = render_bootstrap(for_config)
            for name in managed:
                environment[service_credential_variable(name)] = secrets.token_urlsafe(TOKEN_BYTES)
            for agent in config.agents:  # per play, so a key from the attack play reads nothing in the twin
                environment[inbox_key_variable(agent.id)] = secrets.token_urlsafe(TOKEN_BYTES)
        if serves_tools(config):
            inbox_keys = (
                {agent.id: environment[inbox_key_variable(agent.id)] for agent in config.agents}
                if managed
                else None
            )
            environment[TOOLS_GATEWAY_CONFIG_VARIABLE] = render_tools_gateway_config(
                for_config,
                tokens,
                identities,
                inbox_keys,
            )
        return environment

    return issue_for_play(config), issue_for_play(apply_mode(config, "honest"))


def run_in_stack(run: str, *, mode: str, seed: int | None, robust: bool, logs: Path) -> Path:
    """Play one episode of ``run`` (a config in ``configs/``) in its own compose project; grade it here.

    The episode container plays the episode and, after ``renew_services``, its honest twin (``play_phases``).
    Its run directory is then copied into ``logs`` with each live service's log, in ``services/<name>.log`` of
    the run directory, beside Phoenix's spans (``loc_arena.observability``); the events of the services'
    records are built (``loc_arena.recorded_events``) and the run is graded on this host with the agents' code
    sandboxed, while the gateway still runs, so model-backed monitors reach their model through it and it
    records their calls. Whatever the episode, what the gateway and each live service recorded is copied into
    ``logs`` before the project is removed, after a Ctrl-C too, each log once; when the episode never started,
    a live service that failed to become healthy leaves only its log. A failed renewal before the twin stops
    the run like a failed episode, its logs copied out. The project is kept, stopped, only when a copy or the
    export of Phoenix's spans fails, so nothing recorded is lost. Returns the bundle's directory.
    """
    run = Path(run).name.removesuffix(".yaml")
    config = load_run_config(REPOSITORY / "configs" / f"{run}.yaml")
    settings = config.settings
    instance_id = secrets.token_hex(3)
    project = f"{model_gateway.PROJECT_PREFIX}{instance_id}"
    episode_arguments = ["--mode", mode, "--instance", instance_id, "--play-only", "--next-phase"]
    episode_arguments += [] if robust else ["--minimal"]
    compose_file = logs / "compose" / f"{project}.yaml"
    compose_file.parent.mkdir(parents=True, exist_ok=True)
    compose_file.write_text(
        dump_document(render_compose(config, REPOSITORY, run, episode_arguments), sort_keys=True),
    )
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
    attack_secrets, twin_secrets = issue_secrets(config)
    environment = {**os.environ, **attack_secrets}
    twin_environment = {**os.environ, **twin_secrets}
    run_compose = functools.partial(subprocess.run, env=environment)

    copied = functools.partial(copy_out, run_compose, made=set())
    call_log = model_gateway.locate_call_log(logs, instance_id)
    run_directory = locate_run(config, mode, instance_id, logs)
    service_logs = run_directory / SERVICE_LOGS
    for directory in (call_log.parent, service_logs):
        directory.mkdir(parents=True, exist_ok=True)
    copy_output = [*compose, "cp", f"{EPISODE_SERVICE}:{OUTPUT_DIRECTORY}/.", str(logs)]
    # Each copy adds to its file in this run's own directory: the episode's containers', then the twin's.
    copy_service_logs: list[tuple[list[str], Path | None]] = [
        *list_service_log_copies(config, compose, service_logs),
    ]
    created, spans_left_in_phoenix = False, False
    try:
        if run_compose([*compose, "create", "--build"]).returncode != 0:
            raise StackError("could not build or create the episode's containers: see compose's output above")
        created = True
        # Started on its own, the gateway outlives the episode: the monitors call it while this host grades.
        if not model_gateway.start_gateway(run_compose, compose, settings):
            raise StackError("the gateway did not start to accept calls: see compose's output above")
        exit_code = play_phases(config, compose, mode, service_logs, environment, twin_environment)
        if not copied(copy_output):
            raise StackError("could not copy the episode's run directory out, so it was not graded")
        if exit_code != 0:
            raise StackError(f"the episode exited with code {exit_code}: see its output above")
        # The services' records are copied before grading: the events monitors read are built from them.
        if not all([copied(*copy) for copy in copy_service_logs]):  # every one tried
            raise StackError("could not copy every live service's log out, so the run was not graded")
        build_recorded_run_events(config, run_directory)
        host_token = environment[model_gateway.HOST_TOKEN_VARIABLE]
        return grade_run(
            sandbox_agent_code(config),
            run_directory,
            mode=mode,
            seed=seed,
            monitor_provider=_build_monitor_provider(config, compose, host_token),
        )
    except SpansNotExportedError:
        spans_left_in_phoenix = True
        raise
    finally:
        never_started = [*compose, "ps", "--all", "--status", "created", "--quiet", EPISODE_SERVICE]
        started = created and not run_compose(never_started, capture_output=True, text=True).stdout.strip()
        # An episode that never started has no output or call log, but a live service that failed to become
        # healthy says why in its own.
        pending = [(copy_output, None), *copy_service_logs] if started else copy_service_logs
        # Every copy is tried, a failed one included, before the project is kept. The gateway's record is
        # copied once it holds every call the gateway logged, the monitors' included.
        copies = [copied(*copy) for copy in pending] if created else []
        recorded = not started or model_gateway.copy_call_log(run_compose, compose, settings, call_log)
        if created and (spans_left_in_phoenix or not (all(copies) and recorded)):
            run_compose([*compose, "stop"])
            raise StackError(f"could not copy every log out: kept compose project {project} and its volumes")
        run_compose([*compose, "down", "--volumes", "--remove-orphans"])
        if created and not started:
            raise StackError(NEVER_STARTED.format(logs=service_logs))


def sandbox_agent_code(config: RunConfig) -> RunConfig:
    """``config`` with the agents' code run in a no-network container: always so when grading a stack run."""
    stack = config.settings.stack.model_copy(update={"sandbox_agent_code": True})
    return dataclasses.replace(config, settings=config.settings.model_copy(update={"stack": stack}))


def _build_monitor_provider(config: RunConfig, compose: list[str], token: str) -> OpenRouterProvider | None:
    """The provider of a live run's monitors: the gateway, at the port it publishes on this host's loopback.

    A scripted run's monitors make no model call. A live run's call with the host's ``token`` and never fall
    back to their heuristic for want of a provider: if the gateway's port cannot be found, grading stops.
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
        gateway_token=token,
        model_providers=config.model_providers,
    )
