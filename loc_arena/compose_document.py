"""Render the per-episode compose document from the run config's topology."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml
from scenarios.loader import DEFAULT_CODEBASE, load_run_scenario, load_scenario

from loc_arena.compose_schema import (
    ComposeBuild,
    ComposeDocument,
    ComposeHealthcheck,
    ComposeSecret,
    ComposeService,
    ComposeServiceSecret,
    ComposeServiceVolume,
)
from loc_arena.config import RunConfig, load_run_config
from loc_arena.registry import get_main_task_scorer
from loc_arena.stack.constants import (
    CODEBASE_BUILD_ARGUMENT,
    CONTROL_KEY_FILE_ENVIRONMENT_VARIABLE,
    GATEWAY_CORE_HOSTNAME,
    GATEWAY_EDGE_HOSTNAME,
    GATEWAY_EDGE_URL_ENVIRONMENT_VARIABLE,
    GRADER_HARNESS_MOUNT_PATH,
    GRADER_MEASURE_MODULE_MOUNT_PATH,
    HEALTH_ROUTE,
    IMAGE_TAG_ENVIRONMENT_VARIABLE,
    MAIN_TASK_SCORER_ENVIRONMENT_VARIABLE,
    RECORDER_HOSTNAME,
    SANDBOX_AGENT_ID_ENVIRONMENT_VARIABLE,
    SETTINGS_ENVIRONMENT_VARIABLE,
    build_sandbox_service_name,
)
from loc_arena.stack.settings import DockerSettings, LocArenaSettings
from loc_arena.topology import (
    STACK_VOLUME_MOUNT_PATHS,
    ImageName,
    SecretDeclaration,
    ServiceDeclaration,
    StackTopology,
    VolumeDeclaration,
    load_stack_topology,
)

LABEL = "loc-arena.eval"
APP_IMAGE = "loc-arena-app"
SANDBOX_IMAGE = "loc-arena-sandbox"
LOCAL_IMAGE_PULL_POLICY: Final = "never"  # compose builds a missing image locally
PROJECT_DIRECTORY: Final = Path(__file__).resolve().parent.parent  # relative host paths resolve against it
REFERENCE_RUN_CONFIG: Final = PROJECT_DIRECTORY / "configs" / "aurora-efficiency.yaml"
REFERENCE_COMPOSE_FILE: Final = PROJECT_DIRECTORY / "compose.yaml"
SECRET_FILE_MODE: Final = 0o400  # an environment-sourced secret is readable by its owner, the service's user
LISTEN_HOST: Final = "0.0.0.0"  # every interface of the container: its networks decide who can connect

_HOSTNAME_BY_SERVICE: Final = {
    "gateway_core": GATEWAY_CORE_HOSTNAME,
    "gateway_edge": GATEWAY_EDGE_HOSTNAME,
    "recorder": RECORDER_HOSTNAME,
}


IMAGES: Final[Mapping[ImageName, str]] = {"app": APP_IMAGE, "sandbox": SANDBOX_IMAGE}
_IMAGE_TAG: Final = f"${{{IMAGE_TAG_ENVIRONMENT_VARIABLE}:?the image tag of the episode}}"


def _render_build(image: ImageName, codebase: str) -> ComposeBuild:
    build: ComposeBuild = {"context": ".", "target": image}
    if codebase != DEFAULT_CODEBASE:  # the Dockerfile's default codebase needs no build argument
        build["args"] = {CODEBASE_BUILD_ARGUMENT: codebase}
    return build


@dataclass(frozen=True)
class ServiceSpec:
    """One compose service to render: a ``services:`` entry, or one agent's copy of a ``per_agent`` entry."""

    name: str
    config_name: str  # its key under ``services:``, shared by every sandbox of a per-agent entry
    declaration: ServiceDeclaration

    def for_agent(self, agent_id: str, gateway_edge_url: str) -> ServiceSpec:
        """This per-agent entry as ``agent_id``'s sandbox: named after it, told its agent and model route."""
        environment = {
            **self.declaration.environment,
            SANDBOX_AGENT_ID_ENVIRONMENT_VARIABLE: agent_id,
            GATEWAY_EDGE_URL_ENVIRONMENT_VARIABLE: gateway_edge_url,
        }
        return ServiceSpec(
            name=build_sandbox_service_name(agent_id),
            config_name=self.config_name,
            declaration=self.declaration.model_copy(update={"environment": environment}),
        )


@dataclass(frozen=True)
class GradingInputs:
    """What the grader gets from the run's scenario: its harness files, measure module and scorer name."""

    harness_directory: str  # relative (./...), so the render is the same on every machine
    measure_module: str
    scorer: str


@dataclass(frozen=True)
class RunTopology:
    """What rendering one service needs from the rest of the run."""

    settings: LocArenaSettings
    topology: StackTopology
    service_names: Mapping[str, tuple[str, ...]]  # a config name -> the compose services rendered from it
    grading: GradingInputs | None  # None when the run names no scenario
    codebase: str


def _escape_interpolation(value: str) -> str:
    return value.replace("$", "$$")


def _render_healthcheck(port: int, docker: DockerSettings) -> ComposeHealthcheck:
    url = f"http://localhost:{port}{HEALTH_ROUTE}"
    timeout = docker.healthcheck_request_timeout_seconds
    probe = f"import urllib.request; urllib.request.urlopen({url!r}, timeout={timeout})"
    return {
        "test": ["CMD", "python", "-c", probe],
        "interval": f"{docker.healthcheck_interval_seconds}s",
        "timeout": f"{docker.healthcheck_timeout_seconds}s",
        "retries": docker.healthcheck_retries,
        "start_period": f"{docker.healthcheck_start_period_seconds}s",
    }


def _render_secret_source(source: SecretDeclaration) -> ComposeSecret:
    secret: ComposeSecret = {}
    if source.file is not None:
        secret["file"] = source.file
    if source.environment is not None:
        secret["environment"] = source.environment
    return secret


def _render_secret_grant(spec: ServiceSpec, name: str, source: SecretDeclaration) -> ComposeServiceSecret:
    grant: ComposeServiceSecret = {"source": name, "target": name}
    if source.environment is None:
        return grant  # compose bind-mounts a file source, which keeps the host file's owner and mode
    declaration = spec.declaration
    if declaration.read_only_root_filesystem:
        raise ValueError(
            f"{spec.name} has a read-only root, where compose refuses to write the environment-sourced "
            f"secret {name!r}: give it a file source",
        )
    uid, _, gid = (declaration.user or "").partition(":")
    if not (uid.isdigit() and gid.isdigit()):
        raise ValueError(
            f"{spec.name} needs a numeric user uid:gid to own the secret {name!r}, got {declaration.user!r}",
        )
    grant["uid"], grant["gid"], grant["mode"] = uid, gid, f"0{SECRET_FILE_MODE:o}"
    return grant


def _render_volume_mount(
    name: str,
    volume: VolumeDeclaration,
    spec: ServiceSpec,
) -> ComposeServiceVolume | None:
    """The mount of volume ``name`` in ``spec``, or None when the service is granted no access to it."""
    if spec.config_name not in volume.read_write + volume.read_only:
        return None
    return {
        "type": "volume",
        "source": name,
        "target": STACK_VOLUME_MOUNT_PATHS[name].as_posix(),
        "read_only": spec.config_name in volume.read_only,
    }


def _render_file_bind(source: str, target: Path) -> ComposeServiceVolume:
    return {
        "type": "bind",
        "source": source,
        "target": target.as_posix(),
        "read_only": True,
        # a missing file fails the run instead of mounting a new, empty directory in its place
        "bind": {"create_host_path": False},
    }


def _render_mounts(spec: ServiceSpec, run: RunTopology) -> list[ComposeServiceVolume]:
    mounts = [
        mount
        for name, volume in run.topology.volumes.items()
        if (mount := _render_volume_mount(name, volume, spec)) is not None
    ]
    grading = run.grading
    if not spec.declaration.mounts_grading_harness or grading is None:
        return mounts
    harness = [
        _render_file_bind(f"{grading.harness_directory}/{file_name}", GRADER_HARNESS_MOUNT_PATH / file_name)
        for file_name in run.settings.grading.harness_file_names
    ]
    return [*mounts, *harness, _render_file_bind(grading.measure_module, GRADER_MEASURE_MODULE_MOUNT_PATH)]


def _render_environment(spec: ServiceSpec, run: RunTopology) -> dict[str, str]:
    environment = dict(spec.declaration.environment)
    if spec.declaration.mounts_grading_harness and run.grading is not None:
        environment[MAIN_TASK_SCORER_ENVIRONMENT_VARIABLE] = run.grading.scorer
    environment[SETTINGS_ENVIRONMENT_VARIABLE] = run.settings.model_dump_json()
    return {name: _escape_interpolation(value) for name, value in environment.items()}


def _attach_networks(service: ComposeService, spec: ServiceSpec) -> None:
    hostname = _HOSTNAME_BY_SERVICE.get(spec.name)
    networks = spec.declaration.networks
    if not networks:
        service["network_mode"] = "none"
    elif hostname is None:
        service["networks"] = list(networks)
    else:
        service["networks"] = {network: {"aliases": [hostname]} for network in networks}


def _render_tmpfs(declaration: ServiceDeclaration, docker: DockerSettings) -> list[str]:
    if not declaration.runs_agent_code:
        return list(declaration.tmpfs)
    size = f"size={docker.agent_tmpfs_size_bytes}"
    return [f"{entry},{size}" if ":" in entry else f"{entry}:{size}" for entry in declaration.tmpfs]


def _apply_process(
    service: ComposeService,
    declaration: ServiceDeclaration,
    settings: LocArenaSettings,
) -> None:
    if declaration.app is not None and declaration.port_setting is not None:
        port = int(getattr(settings.gateway, declaration.port_setting))
        service["command"] = [
            "uvicorn",
            "--factory",
            declaration.app,
            "--host",
            LISTEN_HOST,
            "--port",
            str(port),
        ]
        service["healthcheck"] = _render_healthcheck(port, settings.docker)
    elif declaration.command:
        service["command"] = list(declaration.command)
    if declaration.init:
        service["init"] = True


def _apply_limits_and_hardening(
    service: ComposeService,
    declaration: ServiceDeclaration,
    docker: DockerSettings,
) -> None:
    if declaration.user is not None:
        service["user"] = declaration.user
    if declaration.read_only_root_filesystem:
        service["read_only"] = True
    if declaration.tmpfs:
        service["tmpfs"] = _render_tmpfs(declaration, docker)
    if declaration.cap_drop:
        service["cap_drop"] = list(declaration.cap_drop)
    if declaration.security_opt:
        service["security_opt"] = list(declaration.security_opt)
    if declaration.mem_limit is not None:
        service["mem_limit"] = declaration.mem_limit
    if declaration.cpus is not None:
        service["cpus"] = declaration.cpus
    if declaration.pids_limit is not None:
        service["pids_limit"] = declaration.pids_limit


def render_service(spec: ServiceSpec, run: RunTopology) -> ComposeService:
    """Render one compose service."""
    declaration = spec.declaration
    service: ComposeService = {
        "image": f"{IMAGES[declaration.image]}:{_IMAGE_TAG}",
        "build": _render_build(declaration.image, run.codebase),
        "pull_policy": LOCAL_IMAGE_PULL_POLICY,
        "environment": _render_environment(spec, run),
        "labels": {LABEL: "1"},
        "restart": "no",
    }
    _apply_process(service, declaration, run.settings)
    if declaration.profiles:
        service["profiles"] = list(declaration.profiles)  # on-demand (`docker compose run`), not part of `up`
    if declaration.secrets:
        service["secrets"] = [
            _render_secret_grant(spec, name, run.topology.secrets[name]) for name in declaration.secrets
        ]
    _attach_networks(service, spec)
    mounts = _render_mounts(spec, run)
    if mounts:
        service["volumes"] = mounts
    if declaration.depends_on_healthy:
        service["depends_on"] = {
            name: {"condition": "service_healthy"}
            for reference in declaration.depends_on_healthy
            for name in run.service_names[reference]
        }
    _apply_limits_and_hardening(service, declaration, run.settings.docker)
    return service


def _expand_per_agent(templates: Sequence[ServiceSpec], config: RunConfig) -> list[ServiceSpec]:
    edge_url = f"http://{GATEWAY_EDGE_HOSTNAME}:{config.settings.gateway.edge_port}"
    specs: list[ServiceSpec] = []
    for template in templates:
        if template.declaration.per_agent:
            specs.extend(template.for_agent(agent.id, edge_url) for agent in config.agents)
        else:
            specs.append(template)
    return specs


def _relative_to_project(path: Path) -> str:
    return f"./{path.relative_to(PROJECT_DIRECTORY).as_posix()}"


def _read_grading_inputs(config: RunConfig) -> GradingInputs | None:
    if config.scenario is None:
        return None
    scenario = load_scenario(config.scenario)
    return GradingInputs(
        harness_directory=_relative_to_project(scenario.reference_dir),
        measure_module=_relative_to_project(scenario.measure_module),
        scorer=get_main_task_scorer(config).name,
    )


def read_codebase(config: RunConfig) -> str:
    """The run scenario's codebase, which both images are built with: a directory, or ``ValueError``."""
    scenario = load_run_scenario(config.scenario)
    if not scenario.codebase_directory.is_dir():
        raise ValueError(
            f"scenario {scenario.name!r} names the codebase {scenario.codebase!r}, but "
            f"{scenario.codebase_directory} is not a directory",
        )
    return scenario.codebase


def render_compose(config: RunConfig) -> ComposeDocument:
    """Render the compose document for one episode entirely from the resolved config."""
    topology = load_stack_topology(config.raw)
    templates = [
        ServiceSpec(name=name, config_name=name, declaration=declaration)
        for name, declaration in topology.services.items()
    ]
    specs = _expand_per_agent(templates, config)
    run = RunTopology(
        settings=config.settings,
        topology=topology,
        service_names={
            template.config_name: tuple(
                spec.name for spec in specs if spec.config_name == template.config_name
            )
            for template in templates
        },
        grading=_read_grading_inputs(config),
        codebase=read_codebase(config),
    )
    document: ComposeDocument = {
        "services": {spec.name: render_service(spec, run) for spec in specs},
        "networks": {
            name: {"internal": network.internal, "labels": {LABEL: "1"}}
            for name, network in topology.networks.items()
        },
        "volumes": {name: {"labels": {LABEL: "1"}} for name in topology.volumes},
    }
    if topology.secrets:
        document["secrets"] = {
            name: _render_secret_source(source) for name, source in topology.secrets.items()
        }
    return document


def dump_compose_document(document: ComposeDocument) -> str:
    """The document as compose reads it: YAML, in the order it was rendered."""
    return yaml.safe_dump(document, sort_keys=False)


def render_reference_compose_file() -> str:
    """The text of the committed ``compose.yaml``: the reference run config rendered, under a header."""
    run_config = REFERENCE_RUN_CONFIG.relative_to(PROJECT_DIRECTORY)
    header = (
        f"# compose.yaml -- the per-episode LOC-Arena stack, rendered from {run_config}\n"
        "# (which extends configs/env.default.yaml, where the topology lives).\n"
        "# GENERATED: do not edit by hand. Change the config, then regenerate this file with\n"
        "# `uv run python -m loc_arena.compose_document`; a unit test fails while the two differ.\n"
        "# The harness renders its own copy per episode. Every compose command needs\n"
        f"# {CONTROL_KEY_FILE_ENVIRONMENT_VARIABLE}, the path of the episode's control key file, and\n"
        f"# {IMAGE_TAG_ENVIRONMENT_VARIABLE}, the tag of the images the episode builds and runs.\n"
        "\n"
    )
    return header + dump_compose_document(render_compose(load_run_config(REFERENCE_RUN_CONFIG)))


if __name__ == "__main__":
    REFERENCE_COMPOSE_FILE.write_text(render_reference_compose_file())
