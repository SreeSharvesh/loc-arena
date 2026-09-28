"""The stack's topology as the run config declares it: its services, networks, volumes and secrets.

The ``services:``, ``networks:``, ``volumes:`` and ``secrets:`` blocks of the resolved run config (see
``configs/env.default.yaml``) are validated once, at this boundary, into frozen pydantic models;
:mod:`loc_arena.compose_document` renders them into the compose file. A wrong or unknown key fails, and so
does a reference to a name the topology does not declare (a service's network, secret or dependency, a
volume's service): the error names the path of the bad entry (``volumes.sealed_log.read_only.1``), since a
typo would otherwise silently drop a mount, a grant or a hardening option.

References are checked against the names the topology declares, which :func:`load_stack_topology` passes to
pydantic as the validation context (docs.pydantic.dev/latest/concepts/validators/#validation-context).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from loc_arena.stack.constants import MIRROR_MOUNT_PATH, SEALED_MOUNT_PATH, WORKSPACE_MOUNT_PATH
from loc_arena.stack.settings import GatewaySettings

# Where each of the stack's own volumes is mounted: the paths its services' code reads and writes, which the
# images create owned by their non-root user (./Dockerfile).
STACK_VOLUME_MOUNT_PATHS: Final[Mapping[str, Path]] = {
    "sealed_log": SEALED_MOUNT_PATH,
    "mirror_log": MIRROR_MOUNT_PATH,
    "checkout": WORKSPACE_MOUNT_PATH,
}
PORT_SETTING_SUFFIX: Final = "_port"  # a port_setting names a port field of settings.gateway

type ImageName = Literal["app", "sandbox"]  # the ./Dockerfile targets a service can run
type ServiceRole = Literal["sealed", "tamperable", "trusted"]


@dataclass(frozen=True)
class DeclaredNames:
    """The names the topology declares: what its entries may reference (the pydantic validation context)."""

    services: frozenset[str]
    networks: frozenset[str]
    secrets: frozenset[str]


def _read_declared_names(info: ValidationInfo) -> DeclaredNames:
    if not isinstance(info.context, DeclaredNames):
        raise TypeError("validate a topology with load_stack_topology, which declares the names it may use")
    return info.context


def _reference_to(kind: str, declared: Callable[[DeclaredNames], frozenset[str]]) -> AfterValidator:
    """A validator accepting only a name that the topology declares as a ``kind``."""

    def check_reference(name: str, info: ValidationInfo) -> str:
        known = declared(_read_declared_names(info))
        if name not in known:
            raise ValueError(f"unknown {kind} {name!r} (declared: {sorted(known)})")
        return name

    return AfterValidator(check_reference)


def _check_stack_volume(name: str) -> str:
    if name not in STACK_VOLUME_MOUNT_PATHS:
        raise ValueError(
            f"no mount path is known for volume {name!r} (known: {sorted(STACK_VOLUME_MOUNT_PATHS)})",
        )
    return name


ServiceReference = Annotated[str, _reference_to("service", lambda names: names.services)]
NetworkReference = Annotated[str, _reference_to("network", lambda names: names.networks)]
SecretReference = Annotated[str, _reference_to("secret", lambda names: names.secrets)]
VolumeName = Annotated[str, AfterValidator(_check_stack_volume)]


class TopologyEntry(BaseModel):
    """An entry of the topology: immutable, and an unknown key is an error rather than silently ignored."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ServiceDeclaration(TopologyEntry):
    """One ``services:`` entry: what a container runs, what it joins and holds, and how it is hardened.

    An absent key takes the neutral default: no network, no secret, no limit.
    """

    role: ServiceRole | None = Field(
        default=None,
        description="For the reader of the config only: the layer the service belongs to.",
    )
    image: ImageName = Field(description="The ./Dockerfile target the service runs.")
    app: str | None = Field(
        default=None,
        description="The module:factory served by `uvicorn --factory`, on the port named by port_setting.",
    )
    port_setting: str | None = Field(
        default=None,
        description="The settings.gateway field holding the app's port (and so its health probe's).",
    )
    per_agent: bool = Field(
        default=False,
        description="Rendered once per agent of the run, as sandbox-<agent id>.",
    )
    runs_agent_code: bool = Field(
        default=False,
        description="Agents' code runs in it: its tmpfs is capped at settings.docker.agent_tmpfs_size_bytes.",
    )
    networks: tuple[NetworkReference, ...] = Field(
        default=(),
        description="The networks it joins; none turns networking off (network_mode: none).",
    )
    secrets: tuple[SecretReference, ...] = Field(
        default=(),
        description="The secrets it may read, under /run/secrets/<name>.",
    )
    mounts_grading_harness: bool = Field(
        default=False,
        description=(
            "Binds each settings.grading.harness_file_names file and the measure.py of the run's scenario "
            "read-only, and names the run's scorer in LOC_ARENA_MAIN_TASK_SCORER."
        ),
    )
    command: tuple[str, ...] = Field(default=(), description="The command of a service with no app.")
    init: bool = Field(default=False, description="Runs an init process that reaps detached processes.")
    profiles: tuple[str, ...] = Field(
        default=(),
        description="Its compose profiles: a profiled service starts on `docker compose run`, not on `up`.",
    )
    environment: dict[str, str] = Field(default_factory=dict, description="Its environment variables.")
    depends_on_healthy: tuple[ServiceReference, ...] = Field(
        default=(),
        description="The services that must be healthy before it starts (every copy of a per-agent one).",
    )
    user: str | None = Field(default=None, description="The uid:gid its processes run as.")
    read_only_root_filesystem: bool = Field(
        default=False,
        description="Mounts its root filesystem read-only (compose's read_only).",
    )
    tmpfs: tuple[str, ...] = Field(default=(), description="Its tmpfs mounts, as path[:options].")
    cap_drop: tuple[str, ...] = Field(default=(), description="The Linux capabilities it drops.")
    security_opt: tuple[str, ...] = Field(default=(), description="Its security options.")
    mem_limit: str | None = Field(default=None, description="Its memory limit, in compose's byte notation.")
    cpus: float | None = Field(default=None, description="The CPUs it may use.")
    pids_limit: int | None = Field(default=None, description="The most processes it may run at once.")

    @model_validator(mode="after")
    def _check_process(self) -> Self:
        """An app is served by uvicorn on a port of settings.gateway, so it takes a port and no command."""
        if (self.app is None) != (self.port_setting is None):
            raise ValueError("an app needs a port_setting, and a port_setting an app")
        if self.app is not None and self.command:
            raise ValueError("an app is served by uvicorn, so it takes no command")
        if self.port_setting is not None and not (
            self.port_setting.endswith(PORT_SETTING_SUFFIX)
            and self.port_setting in GatewaySettings.model_fields
        ):
            raise ValueError(f"port_setting must name a port of settings.gateway, got {self.port_setting!r}")
        return self


class NetworkDeclaration(TopologyEntry):
    """One ``networks:`` entry."""

    internal: bool = Field(
        default=True,
        description="Whether the network has no route out of the host: every network but the core's egress.",
    )


class VolumeDeclaration(TopologyEntry):
    """One ``volumes:`` entry: a named volume, and the services writing and reading it."""

    read_write: tuple[ServiceReference, ...] = Field(default=(), description="The services that write it.")
    read_only: tuple[ServiceReference, ...] = Field(
        default=(),
        description="The services that read it, mounted read-only.",
    )

    @model_validator(mode="after")
    def _check_one_access_per_service(self) -> Self:
        both = set(self.read_write) & set(self.read_only)
        if both:
            raise ValueError(f"a service either writes or reads a volume, not both: {sorted(both)}")
        return self


class SecretDeclaration(TopologyEntry):
    """One ``secrets:`` entry: where compose reads the secret's value, a file or its own environment."""

    file: str | None = Field(default=None, description="The host file holding the value.")
    environment: str | None = Field(
        default=None,
        description="The variable of the `docker compose` process holding the value.",
    )

    @model_validator(mode="after")
    def _check_one_source(self) -> Self:
        if (self.file is None) == (self.environment is None):
            raise ValueError("a secret comes from exactly one of a file or an environment variable")
        return self


class StackTopology(TopologyEntry):
    """The stack's topology blocks of the resolved run config, validated."""

    services: dict[str, ServiceDeclaration] = Field(description="The stack's services, by config name.")
    networks: dict[str, NetworkDeclaration] = Field(description="The stack's networks.")
    volumes: dict[VolumeName, VolumeDeclaration] = Field(description="The stack's named volumes.")
    secrets: dict[str, SecretDeclaration] = Field(
        default_factory=dict,
        description="The stack's secrets, readable under /run/secrets by the services granted them.",
    )


def _declared_keys(raw: Mapping[str, object], block: str) -> frozenset[str]:
    """The names declared under ``block`` of ``raw``: none when it is no mapping (the model then says why)."""
    entries = raw.get(block)
    return frozenset(str(name) for name in entries) if isinstance(entries, Mapping) else frozenset()


def load_stack_topology(raw: Mapping[str, object]) -> StackTopology:
    """Validate the topology blocks of the resolved run config ``raw``; other keys are left to their owners.

    Raises pydantic's ``ValidationError`` (a ``ValueError``) naming the path of every bad entry.
    """
    declared = DeclaredNames(
        services=_declared_keys(raw, "services"),
        networks=_declared_keys(raw, "networks"),
        secrets=_declared_keys(raw, "secrets"),
    )
    blocks = {name: raw[name] for name in StackTopology.model_fields if name in raw}
    return StackTopology.model_validate(blocks, context=declared)
