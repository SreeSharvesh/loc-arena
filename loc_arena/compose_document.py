"""Render the per-episode compose document from the run config's topology."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NotRequired, TypedDict

import yaml
from scenarios.loader import load_scenario

from loc_arena.config import RunConfig, load_run_config
from loc_arena.stack.constants import (
    CONTROL_KEY_FILE_ENVIRONMENT_VARIABLE,
    GATEWAY_CORE_HOSTNAME,
    GATEWAY_EDGE_HOSTNAME,
    GATEWAY_EDGE_URL_ENVIRONMENT_VARIABLE,
    GRADER_HARNESS_MOUNT_PATH,
    HEALTH_ROUTE,
    IMAGE_TAG_ENVIRONMENT_VARIABLE,
    MIRROR_MOUNT_PATH,
    RECORDER_HOSTNAME,
    SANDBOX_AGENT_ID_ENVIRONMENT_VARIABLE,
    SEALED_MOUNT_PATH,
    SETTINGS_ENVIRONMENT_VARIABLE,
    WORKSPACE_MOUNT_PATH,
    build_sandbox_service_name,
)
from loc_arena.stack.settings import DockerSettings, GatewaySettings, LocArenaSettings

LABEL = "loc-arena.eval"
APP_IMAGE = "loc-arena-app"
SANDBOX_IMAGE = "loc-arena-sandbox"
LOCAL_IMAGE_PULL_POLICY: Final = "never"  # compose builds a missing image locally
PROJECT_DIRECTORY: Final = Path(__file__).resolve().parent.parent  # relative host paths resolve against it
REFERENCE_RUN_CONFIG: Final = PROJECT_DIRECTORY / "configs" / "aurora-efficiency.yaml"
REFERENCE_COMPOSE_FILE: Final = PROJECT_DIRECTORY / "compose.yaml"
SECRET_FILE_MODE: Final = 0o400  # an environment-sourced secret is readable by its owner, the service's user
LISTEN_HOST: Final = "0.0.0.0"  # every interface of the container: its networks decide who can connect

# Where each named volume is mounted: the paths the services' code reads and writes.
_MOUNT_PATH_BY_VOLUME: Final = {
    "sealed_log": SEALED_MOUNT_PATH,
    "mirror_log": MIRROR_MOUNT_PATH,
    "checkout": WORKSPACE_MOUNT_PATH,
}
_HOSTNAME_BY_SERVICE: Final = {
    "gateway_core": GATEWAY_CORE_HOSTNAME,
    "gateway_edge": GATEWAY_EDGE_HOSTNAME,
    "recorder": RECORDER_HOSTNAME,
}


class ComposeBuild(TypedDict):
    """A service's ``build``: the context, and the stage of a multi-stage Dockerfile."""

    context: str
    target: NotRequired[str]


class ComposeHealthcheck(TypedDict):
    """A service ``healthcheck``."""

    test: list[str]
    interval: str
    timeout: str
    retries: int
    start_period: str


class ComposeDependency(TypedDict):
    """One ``depends_on`` entry (long syntax)."""

    condition: str


class ComposeNetworkAttachment(TypedDict, total=False):
    """A service's attachment to one network (long syntax)."""

    aliases: list[str]


class ComposeServiceSecret(TypedDict):
    """One secret grant (long syntax). Compose applies uid, gid and mode to an environment source only."""

    source: str
    target: str
    uid: NotRequired[str]
    gid: NotRequired[str]
    mode: NotRequired[str]  # octal digits: compose-go parses a string mode in base 8


class ComposeBindOptions(TypedDict):
    """A bind mount's options."""

    create_host_path: bool


class ComposeServiceVolume(TypedDict):
    """One mount of a service (long syntax)."""

    type: str
    source: str
    target: str
    read_only: bool
    bind: NotRequired[ComposeBindOptions]


class ComposeService(TypedDict, total=False):
    """The service fields this harness renders."""

    image: str
    build: ComposeBuild
    pull_policy: str
    command: list[str]
    init: bool
    profiles: list[str]
    user: str
    environment: dict[str, str]
    labels: dict[str, str]
    restart: str
    secrets: list[ComposeServiceSecret]
    networks: list[str] | dict[str, ComposeNetworkAttachment]
    network_mode: str
    volumes: list[ComposeServiceVolume]
    healthcheck: ComposeHealthcheck
    depends_on: dict[str, ComposeDependency]
    read_only: bool
    tmpfs: list[str]
    cap_drop: list[str]
    security_opt: list[str]
    mem_limit: str
    cpus: float
    pids_limit: int


class ComposeNetwork(TypedDict):
    """A top-level network."""

    internal: bool
    labels: dict[str, str]


class ComposeVolume(TypedDict):
    """A top-level named volume."""

    labels: dict[str, str]


class ComposeSecret(TypedDict, total=False):
    """A top-level secret: its value comes from a file or from the compose process's environment."""

    file: str
    environment: str


class ComposeDocument(TypedDict, total=False):
    """The rendered compose document."""

    services: dict[str, ComposeService]
    networks: dict[str, ComposeNetwork]
    volumes: dict[str, ComposeVolume]
    secrets: dict[str, ComposeSecret]


IMAGES: Final = {"app": APP_IMAGE, "sandbox": SANDBOX_IMAGE}
_IMAGE_TAG: Final = f"${{{IMAGE_TAG_ENVIRONMENT_VARIABLE}:?the image tag of the episode}}"


def _strings(raw: Mapping[str, object], key: str, where: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{where}.{key} must be a list, got {value!r}")
    return tuple(str(item) for item in value)


def _optional_string(raw: Mapping[str, object], key: str) -> str | None:
    return str(raw[key]) if key in raw else None


def _flag(raw: Mapping[str, object], key: str, where: str) -> bool:
    value = raw.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"{where}.{key} must be true or false, got {value!r}")
    return value


def _reject_unknown_keys(raw: Mapping[str, object], known: frozenset[str], where: str) -> None:
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"{where} has unknown keys {sorted(unknown)} (known: {sorted(known)})")


def _port_setting(raw: Mapping[str, object], where: str) -> str | None:
    """The settings.gateway field holding the port of the service's app, which is also its command."""
    port_setting = _optional_string(raw, "port_setting")
    if ("app" in raw) != (port_setting is not None):
        raise ValueError(f"{where}: an app needs a port_setting, and a port_setting an app")
    if "app" in raw and "command" in raw:
        raise ValueError(f"{where}: an app is served by uvicorn, so it takes no command")
    if port_setting is not None and not (
        port_setting.endswith("_port") and port_setting in GatewaySettings.model_fields
    ):
        raise ValueError(f"{where}.port_setting must name a port of settings.gateway, got {port_setting!r}")
    return port_setting


@dataclass(frozen=True)
class ServiceSpec:
    """One ``services:`` entry of the run config, typed. Absent keys take the neutral default."""

    name: str
    config_name: str  # its key under ``services:``, shared by every sandbox of a per-agent entry
    image: str
    app: str | None  # the module:factory uvicorn serves
    port_setting: str | None  # the settings.gateway field holding the app's port
    per_agent: bool
    runs_agent_code: bool  # its tmpfs is capped at settings.docker.agent_tmpfs_size_bytes
    networks: tuple[str, ...]
    secrets: tuple[str, ...]
    mounts_grading_harness: bool
    command: tuple[str, ...]
    init: bool
    profiles: tuple[str, ...]
    environment: Mapping[str, str]
    depends_on_healthy: tuple[str, ...]
    user: str | None
    read_only_root_filesystem: bool
    tmpfs: tuple[str, ...]
    cap_drop: tuple[str, ...]
    security_opt: tuple[str, ...]
    mem_limit: str | None
    cpus: float | None
    pids_limit: int | None

    @classmethod
    def from_config(cls, name: str, raw: Mapping[str, object]) -> ServiceSpec:
        """Parse the YAML mapping under ``services.<name>``; a wrong or unknown key raises ``ValueError``."""
        where = f"services.{name}"
        _reject_unknown_keys(raw, _SERVICE_KEYS, where)
        image = raw.get("image")
        if not isinstance(image, str) or image not in IMAGES:
            raise ValueError(f"{where}.image must be one of {sorted(IMAGES)}, got {image!r}")
        environment, cpus, pids_limit = raw.get("environment", {}), raw.get("cpus"), raw.get("pids_limit")
        if not isinstance(environment, dict):
            raise ValueError(f"{where}.environment must be a mapping, got {environment!r}")
        return cls(
            name=name,
            config_name=name,
            image=image,
            app=_optional_string(raw, "app"),
            port_setting=_port_setting(raw, where),
            per_agent=_flag(raw, "per_agent", where),
            runs_agent_code=_flag(raw, "runs_agent_code", where),
            networks=_strings(raw, "networks", where),
            secrets=_strings(raw, "secrets", where),
            mounts_grading_harness=_flag(raw, "mounts_grading_harness", where),
            command=_strings(raw, "command", where),
            init=_flag(raw, "init", where),
            profiles=_strings(raw, "profiles", where),
            environment={str(key): str(value) for key, value in environment.items()},
            depends_on_healthy=_strings(raw, "depends_on_healthy", where),
            user=_optional_string(raw, "user"),
            read_only_root_filesystem=_flag(raw, "read_only_root_filesystem", where),
            tmpfs=_strings(raw, "tmpfs", where),
            cap_drop=_strings(raw, "cap_drop", where),
            security_opt=_strings(raw, "security_opt", where),
            mem_limit=_optional_string(raw, "mem_limit"),
            cpus=float(cpus) if isinstance(cpus, int | float) else None,
            pids_limit=pids_limit if isinstance(pids_limit, int) else None,
        )

    def for_agent(self, agent_id: str, gateway_edge_url: str) -> ServiceSpec:
        """This per-agent entry as ``agent_id``'s sandbox: named after it, told its agent and model route."""
        return dataclasses.replace(
            self,
            name=build_sandbox_service_name(agent_id),
            environment={
                **self.environment,
                SANDBOX_AGENT_ID_ENVIRONMENT_VARIABLE: agent_id,
                GATEWAY_EDGE_URL_ENVIRONMENT_VARIABLE: gateway_edge_url,
            },
        )


_SERVICE_KEYS: Final = frozenset(
    field.name for field in dataclasses.fields(ServiceSpec) if field.name not in {"name", "config_name"}
)


@dataclass(frozen=True)
class VolumeSpec:
    """One ``volumes:`` entry of the run config: a named volume, the services writing and reading it."""

    name: str
    read_write: tuple[str, ...]
    read_only: tuple[str, ...]

    @classmethod
    def from_config(cls, name: str, raw: Mapping[str, object]) -> VolumeSpec:
        """Parse the YAML mapping under ``volumes.<name>``; a wrong or unknown key raises ``ValueError``."""
        where = f"volumes.{name}"
        _reject_unknown_keys(raw, frozenset({"read_write", "read_only"}), where)
        if name not in _MOUNT_PATH_BY_VOLUME:
            raise ValueError(
                f"{where}: no mount path is known for it (known: {sorted(_MOUNT_PATH_BY_VOLUME)})",
            )
        volume = cls(name, _strings(raw, "read_write", where), _strings(raw, "read_only", where))
        if set(volume.read_write) & set(volume.read_only):
            raise ValueError(f"{where}: a service either writes or reads it, not both")
        return volume

    @property
    def mount_path(self) -> Path:
        """Where the volume is mounted inside a container."""
        return _MOUNT_PATH_BY_VOLUME[self.name]

    def render_mount(self, service: ServiceSpec) -> ComposeServiceVolume | None:
        """The mount of this volume in ``service``, or None when the service is granted no access to it."""
        if service.config_name not in self.read_write + self.read_only:
            return None
        return {
            "type": "volume",
            "source": self.name,
            "target": self.mount_path.as_posix(),
            "read_only": service.config_name in self.read_only,
        }


@dataclass(frozen=True)
class RunTopology:
    """What rendering one service needs from the rest of the run."""

    settings: LocArenaSettings
    volumes: tuple[VolumeSpec, ...]
    secret_sources: Mapping[str, ComposeSecret]
    service_names: Mapping[str, tuple[str, ...]]  # a config name -> the compose services rendered from it
    harness_directory: str | None  # the scenario's sealed reference directory, relative to the project


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


def _render_secret_grant(spec: ServiceSpec, name: str, source: ComposeSecret) -> ComposeServiceSecret:
    grant: ComposeServiceSecret = {"source": name, "target": name}
    if "environment" not in source:
        return grant  # compose bind-mounts a file source, which keeps the host file's owner and mode
    if spec.read_only_root_filesystem:
        raise ValueError(
            f"{spec.name} has a read-only root, where compose refuses to write the environment-sourced "
            f"secret {name!r}: give it a file source",
        )
    uid, _, gid = (spec.user or "").partition(":")
    if not (uid.isdigit() and gid.isdigit()):
        raise ValueError(
            f"{spec.name} needs a numeric user uid:gid to own the secret {name!r}, got {spec.user!r}",
        )
    grant["uid"], grant["gid"], grant["mode"] = uid, gid, f"0{SECRET_FILE_MODE:o}"
    return grant


def _render_mounts(spec: ServiceSpec, topology: RunTopology) -> list[ComposeServiceVolume]:
    mounts = [mount for volume in topology.volumes if (mount := volume.render_mount(spec)) is not None]
    if not spec.mounts_grading_harness or topology.harness_directory is None:
        return mounts
    return mounts + [
        {
            "type": "bind",
            "source": f"{topology.harness_directory}/{file_name}",
            "target": (GRADER_HARNESS_MOUNT_PATH / file_name).as_posix(),
            "read_only": True,
            # a missing file fails the run instead of mounting a new, empty directory in its place
            "bind": {"create_host_path": False},
        }
        for file_name in topology.settings.grading.harness_file_names
    ]


def _attach_networks(service: ComposeService, spec: ServiceSpec) -> None:
    hostname = _HOSTNAME_BY_SERVICE.get(spec.name)
    if not spec.networks:
        service["network_mode"] = "none"
    elif hostname is None:
        service["networks"] = list(spec.networks)
    else:
        service["networks"] = {network: {"aliases": [hostname]} for network in spec.networks}


def _render_tmpfs(spec: ServiceSpec, docker: DockerSettings) -> list[str]:
    if not spec.runs_agent_code:
        return list(spec.tmpfs)
    size = f"size={docker.agent_tmpfs_size_bytes}"
    return [f"{entry},{size}" if ":" in entry else f"{entry}:{size}" for entry in spec.tmpfs]


def _apply_process(service: ComposeService, spec: ServiceSpec, settings: LocArenaSettings) -> None:
    if spec.app is not None and spec.port_setting is not None:
        port = int(getattr(settings.gateway, spec.port_setting))
        service["command"] = ["uvicorn", "--factory", spec.app, "--host", LISTEN_HOST, "--port", str(port)]
        service["healthcheck"] = _render_healthcheck(port, settings.docker)
    elif spec.command:
        service["command"] = list(spec.command)
    if spec.init:
        service["init"] = True


def _apply_limits_and_hardening(service: ComposeService, spec: ServiceSpec, docker: DockerSettings) -> None:
    if spec.user is not None:
        service["user"] = spec.user
    if spec.read_only_root_filesystem:
        service["read_only"] = True
    if spec.tmpfs:
        service["tmpfs"] = _render_tmpfs(spec, docker)
    if spec.cap_drop:
        service["cap_drop"] = list(spec.cap_drop)
    if spec.security_opt:
        service["security_opt"] = list(spec.security_opt)
    if spec.mem_limit is not None:
        service["mem_limit"] = spec.mem_limit
    if spec.cpus is not None:
        service["cpus"] = spec.cpus
    if spec.pids_limit is not None:
        service["pids_limit"] = spec.pids_limit


def render_service(spec: ServiceSpec, topology: RunTopology) -> ComposeService:
    """Render one compose service."""
    environment = {**spec.environment, SETTINGS_ENVIRONMENT_VARIABLE: topology.settings.model_dump_json()}
    service: ComposeService = {
        "image": f"{IMAGES[spec.image]}:{_IMAGE_TAG}",
        "build": {"context": ".", "target": spec.image},
        "pull_policy": LOCAL_IMAGE_PULL_POLICY,
        "environment": {name: _escape_interpolation(value) for name, value in environment.items()},
        "labels": {LABEL: "1"},
        "restart": "no",
    }
    _apply_process(service, spec, topology.settings)
    if spec.profiles:
        service["profiles"] = list(spec.profiles)  # on-demand (`docker compose run`), not part of `up`
    if spec.secrets:
        service["secrets"] = [
            _render_secret_grant(spec, name, topology.secret_sources[name]) for name in spec.secrets
        ]
    _attach_networks(service, spec)
    mounts = _render_mounts(spec, topology)
    if mounts:
        service["volumes"] = mounts
    if spec.depends_on_healthy:
        service["depends_on"] = {
            name: {"condition": "service_healthy"}
            for reference in spec.depends_on_healthy
            for name in topology.service_names[reference]
        }
    _apply_limits_and_hardening(service, spec, topology.settings.docker)
    return service


def _expand_per_agent(templates: Sequence[ServiceSpec], config: RunConfig) -> list[ServiceSpec]:
    edge_url = f"http://{GATEWAY_EDGE_HOSTNAME}:{config.settings.gateway.edge_port}"
    specs: list[ServiceSpec] = []
    for template in templates:
        if template.per_agent:
            specs.extend(template.for_agent(agent.id, edge_url) for agent in config.agents)
        else:
            specs.append(template)
    return specs


def _check_references(
    templates: Sequence[ServiceSpec],
    volumes: Sequence[VolumeSpec],
    secret_sources: Mapping[str, ComposeSecret],
) -> None:
    """Fail on a name the config uses but does not define (a typo would otherwise drop a mount or a grant)."""
    config_names = {template.config_name for template in templates}
    for template in templates:
        for dependency in set(template.depends_on_healthy) - config_names:
            raise ValueError(f"services.{template.config_name} depends on the unknown service {dependency!r}")
        for secret in set(template.secrets) - set(secret_sources):
            raise ValueError(f"services.{template.config_name} is granted the undeclared secret {secret!r}")
    for volume in volumes:
        for service in set(volume.read_write + volume.read_only) - config_names:
            raise ValueError(f"volumes.{volume.name} names the unknown service {service!r}")


def _relative_harness_directory(config: RunConfig) -> str | None:
    if config.scenario is None:
        return None
    reference_directory = load_scenario(config.scenario).reference_dir
    return f"./{reference_directory.relative_to(PROJECT_DIRECTORY).as_posix()}"


def render_compose(config: RunConfig) -> ComposeDocument:
    """Render the compose document for one episode entirely from the resolved config."""
    raw = config.raw
    templates = [ServiceSpec.from_config(name, entry) for name, entry in raw["services"].items()]
    volumes = tuple(VolumeSpec.from_config(name, entry) for name, entry in raw["volumes"].items())
    secret_sources: dict[str, ComposeSecret] = {
        name: ComposeSecret(**source) for name, source in raw.get("secrets", {}).items()
    }
    _check_references(templates, volumes, secret_sources)
    specs = _expand_per_agent(templates, config)
    topology = RunTopology(
        settings=config.settings,
        volumes=volumes,
        secret_sources=secret_sources,
        service_names={
            template.config_name: tuple(
                spec.name for spec in specs if spec.config_name == template.config_name
            )
            for template in templates
        },
        harness_directory=_relative_harness_directory(config),
    )
    document: ComposeDocument = {
        "services": {spec.name: render_service(spec, topology) for spec in specs},
        "networks": {
            name: {
                "internal": bool(spec.get("internal", True)) if isinstance(spec, dict) else True,
                "labels": {LABEL: "1"},
            }
            for name, spec in raw["networks"].items()
        },
        "volumes": {volume.name: {"labels": {LABEL: "1"}} for volume in volumes},
    }
    if secret_sources:
        document["secrets"] = secret_sources
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
