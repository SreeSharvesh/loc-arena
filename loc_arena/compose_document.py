"""Render the per-episode docker compose document from the resolved run config (config over code).

Each ``services:`` entry of the config is parsed once into a typed :class:`ServiceSpec`; rendering then maps
specs onto the Compose Specification's service fields (docs.docker.com/reference/compose-file/services).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypedDict

from loc_arena.config import RunConfig
from loc_arena.gateway.provider_endpoint import MODEL_ALLOWLIST_ENV, ModelAllowlist, build_model_allowlist
from loc_arena.gateway.provider_key import PROVIDER_KEY_SECRET_NAME

LABEL = "loc-arena.eval"
SERVICE_IMAGE = "loc-arena-svc:latest"  # the stdlib-only placeholder/recorder image (loc_arena/services)
APP_IMAGE = "loc-arena-app:latest"  # the full app: the episode runner and the gateway_core provider endpoint

# where each named volume is mounted inside a container
_VOLUME_MOUNT_PATH = {
    "sealed_log": "/sealed",
    "mirror_log": "/mirror",
    "repos": "/repos",
    "weights_data": "/weights",
}
# config service name -> the role the placeholder image runs (anything else answers health checks)
_APP_ROLE_BY_SERVICE = {"recorder": "recorder", "evidence_reader": "reader", "runner": "runner"}
_ROLES_WITHOUT_SERVER = {"reader", "runner"}  # no health server, so no healthcheck
_DEFAULT_PORT = 8000


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


class ComposeService(TypedDict, total=False):
    """The service fields this harness renders."""

    image: str
    command: list[str]
    profiles: list[str]
    environment: dict[str, str]
    labels: dict[str, str]
    restart: str
    secrets: list[str]
    networks: list[str] | dict[str, ComposeNetworkAttachment]
    network_mode: str
    volumes: list[str]
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


def _strings(raw: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = raw.get(key)
    return tuple(str(item) for item in value) if isinstance(value, list) else ()


@dataclass(frozen=True)
class ServiceSpec:
    """One ``services:`` entry of the run config, typed. Absent keys take the neutral default."""

    name: str
    networks: tuple[str, ...]
    port: int
    uses_app_image: bool
    command: tuple[str, ...]
    profiles: tuple[str, ...]
    environment: Mapping[str, str]
    network_aliases: Mapping[str, tuple[str, ...]]
    depends_on_healthy: tuple[str, ...]
    serves_provider_endpoint: bool
    mounts_sealed_log_read_only: bool
    read_only_root_filesystem: bool
    tmpfs: tuple[str, ...]
    cap_drop: tuple[str, ...]
    security_opt: tuple[str, ...]
    mem_limit: str | None
    cpus: float | None
    pids_limit: int | None

    @classmethod
    def from_config(cls, name: str, raw: Mapping[str, object]) -> ServiceSpec:
        """Parse one config entry (the YAML mapping under ``services.<name>``)."""
        port, cpus, pids_limit = raw.get("port"), raw.get("cpus"), raw.get("pids_limit")
        environment = raw.get("environment")
        aliases = raw.get("network_aliases")
        return cls(
            name=name,
            networks=_strings(raw, "networks"),
            port=port if isinstance(port, int) else _DEFAULT_PORT,
            uses_app_image=raw.get("image") == "app",
            command=_strings(raw, "command"),
            profiles=_strings(raw, "profiles"),
            environment={str(k): str(v) for k, v in environment.items()}
            if isinstance(environment, dict)
            else {},
            network_aliases={str(k): tuple(map(str, v)) for k, v in aliases.items()}
            if isinstance(aliases, dict)
            else {},
            depends_on_healthy=_strings(raw, "depends_on_healthy"),
            serves_provider_endpoint=raw.get("serves_provider_endpoint") is True,
            mounts_sealed_log_read_only=raw.get("read_only") is True,
            read_only_root_filesystem=raw.get("read_only_root_filesystem") is True,
            tmpfs=_strings(raw, "tmpfs"),
            cap_drop=_strings(raw, "cap_drop"),
            security_opt=_strings(raw, "security_opt"),
            mem_limit=str(raw["mem_limit"]) if "mem_limit" in raw else None,
            cpus=float(cpus) if isinstance(cpus, int | float) else None,
            pids_limit=pids_limit if isinstance(pids_limit, int) else None,
        )

    @property
    def app_role(self) -> str:
        """The role the placeholder image runs for this service (``SVC_ROLE``)."""
        return _APP_ROLE_BY_SERVICE.get(self.name, "health")


@dataclass(frozen=True)
class VolumeSpec:
    """One ``volumes:`` entry of the run config: a named volume and the services it is mounted into."""

    name: str
    mount_into: tuple[str, ...]

    @property
    def mount_path(self) -> str:
        """Where the volume is mounted inside a container."""
        return _VOLUME_MOUNT_PATH.get(self.name, f"/{self.name}")


def _healthcheck(port: int) -> ComposeHealthcheck:
    url = f"http://localhost:{port}/health"
    probe = (
        "import urllib.request,sys;"
        f"u=urllib.request.urlopen('{url}',timeout=2);"
        "sys.exit(0 if u.status==200 else 1)"
    )
    return {
        "test": ["CMD", "python", "-c", probe],
        "interval": "3s",
        "timeout": "3s",
        "retries": 15,
        "start_period": "2s",
    }


def _service_volumes(spec: ServiceSpec, volumes: list[VolumeSpec]) -> list[str]:
    mounts = []
    for volume in volumes:
        if spec.name in volume.mount_into:
            read_only = ":ro" if spec.mounts_sealed_log_read_only and volume.name == "sealed_log" else ""
            mounts.append(f"{volume.name}:{volume.mount_path}{read_only}")
    return mounts


def _service_environment(
    spec: ServiceSpec,
    volumes: list[VolumeSpec],
    allowlist: ModelAllowlist,
) -> dict[str, str]:
    environment = {"SVC_NAME": spec.name, "SVC_ROLE": spec.app_role, "SVC_PORT": str(spec.port)}
    environment.update(spec.environment)
    if spec.serves_provider_endpoint:
        environment[MODEL_ALLOWLIST_ENV] = json.dumps(allowlist)
    for volume in volumes:
        if volume.name == "sealed_log" and spec.name in volume.mount_into:
            environment["SEALED_LOG"] = f"{volume.mount_path}/events.jsonl"
    return environment


def _attach_networks(service: ComposeService, spec: ServiceSpec) -> None:
    if not spec.networks:
        service["network_mode"] = "none"  # the networkless evidence-reader
    elif spec.network_aliases:
        service["networks"] = {
            network: {"aliases": list(spec.network_aliases[network])}
            if network in spec.network_aliases
            else {}
            for network in spec.networks
        }
    else:
        service["networks"] = list(spec.networks)


def _apply_limits_and_hardening(service: ComposeService, spec: ServiceSpec) -> None:
    if spec.read_only_root_filesystem:
        service["read_only"] = True
    if spec.tmpfs:
        service["tmpfs"] = list(spec.tmpfs)
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


def render_service(spec: ServiceSpec, volumes: list[VolumeSpec], allowlist: ModelAllowlist) -> ComposeService:
    """Render one service.

    No fixed container_name: compose names containers per project, so episodes can run side by side.
    """
    service: ComposeService = {
        "image": APP_IMAGE if spec.uses_app_image else SERVICE_IMAGE,
        "environment": _service_environment(spec, volumes, allowlist),
        "labels": {LABEL: "1"},
        "restart": "no",
    }
    if spec.command:
        service["command"] = list(spec.command)
    if spec.profiles:
        service["profiles"] = list(spec.profiles)  # on-demand (`docker compose run`), not part of `up`
    if spec.serves_provider_endpoint:
        service["secrets"] = [PROVIDER_KEY_SECRET_NAME]  # /run/secrets/<name>, in this service only
    _attach_networks(service, spec)
    mounts = _service_volumes(spec, volumes)
    if mounts:
        service["volumes"] = mounts
    if spec.app_role not in _ROLES_WITHOUT_SERVER:
        service["healthcheck"] = _healthcheck(spec.port)
    if spec.depends_on_healthy:
        service["depends_on"] = {name: {"condition": "service_healthy"} for name in spec.depends_on_healthy}
    _apply_limits_and_hardening(service, spec)
    return service


def render_compose(config: RunConfig) -> ComposeDocument:
    """Render the compose document for one episode entirely from the resolved config."""
    raw = config.raw
    volumes = [
        VolumeSpec(name, tuple(map(str, spec.get("mount_into", []))) if isinstance(spec, dict) else ())
        for name, spec in raw["volumes"].items()
    ]
    allowlist = build_model_allowlist(config.models)
    document: ComposeDocument = {
        "services": {
            name: render_service(ServiceSpec.from_config(name, spec), volumes, allowlist)
            for name, spec in raw["services"].items()
        },
        "networks": {
            name: {
                "internal": bool(spec.get("internal", True)) if isinstance(spec, dict) else True,
                "labels": {LABEL: "1"},
            }
            for name, spec in raw["networks"].items()
        },
        "volumes": {volume.name: {"labels": {LABEL: "1"}} for volume in volumes},
    }
    secret_sources = raw.get("secrets", {})
    serves_endpoint = any(spec.get("serves_provider_endpoint") is True for spec in raw["services"].values())
    if serves_endpoint and PROVIDER_KEY_SECRET_NAME not in secret_sources:
        raise ValueError(f"the provider endpoint needs a compose secret named {PROVIDER_KEY_SECRET_NAME!r}")
    if secret_sources:
        document["secrets"] = {name: ComposeSecret(**source) for name, source in secret_sources.items()}
    return document
