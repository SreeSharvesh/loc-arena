"""The stack's topology as the run config declares it: services, networks, volumes, secrets, agent groups.

The ``services:``, ``networks:``, ``volumes:``, ``secrets:`` and ``agent_groups:`` blocks of the resolved run
config (see ``configs/env.default.yaml``) are validated once, at this boundary, into frozen pydantic models;
:mod:`loc_arena.compose_document` renders them into the compose file. A wrong or unknown key fails, and so
does a reference to a name the topology or the run does not declare (a service's network, secret or
dependency, a volume's service, an agent id, a group): the error names the path of the bad entry
(``volumes.board.read_write.0.agents.groups.1``), since a typo would otherwise silently drop a mount, a grant
or a hardening option.

Networks and volumes are granted to services by config name (``sandbox`` naming every agent's sandbox) or to
chosen agents, by id or by named group (an :class:`AgentSelection`), which selects those agents' copies of
the ``per_agent`` services. A ``per_agent`` network or volume is itself rendered once per agent, as
``<name>-<agent id>`` (:func:`build_agent_copy_name`): an agent's copy of a per-agent service gets only that
agent's copy of it, and any other service granted it gets every copy.

References are checked against the declared names, which :func:`load_stack_topology` passes to pydantic as
the validation context (docs.pydantic.dev/latest/concepts/validators/#validation-context); a grant is either
a service name or an agent selection, told apart before validation by a callable discriminator
(docs.pydantic.dev/latest/concepts/unions/#discriminated-unions-with-callable-discriminator), so a bad entry
gets one error, not one per union member.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    PositiveInt,
    Tag,
    ValidationInfo,
    field_validator,
    model_validator,
)

from loc_arena.stack.constants import MIRROR_MOUNT_PATH, SEALED_MOUNT_PATH, WORKSPACE_MOUNT_PATH
from loc_arena.stack.settings import GatewaySettings

# Where each of the stack's own volumes is mounted: the paths its services' code reads and writes, which the
# images create owned by their non-root user (./Dockerfile), so Docker hands each fresh volume to that user.
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
    agents: frozenset[str]  # the ids of the run's agents (the run config's agents:)
    groups: frozenset[str]


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


ServiceReference = Annotated[str, _reference_to("service", lambda names: names.services)]
NetworkReference = Annotated[str, _reference_to("network", lambda names: names.networks)]
SecretReference = Annotated[str, _reference_to("secret", lambda names: names.secrets)]
AgentReference = Annotated[str, _reference_to("agent", lambda names: names.agents)]
GroupReference = Annotated[str, _reference_to("agent group", lambda names: names.groups)]


def build_agent_copy_name(name: str, agent_id: str) -> str:
    """The name of ``agent_id``'s copy of the per-agent network or volume ``name``."""
    return f"{name}-{agent_id}"


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


class AgentSelection(TopologyEntry):
    """Chosen agents of the run, by id and by group: a grant to them reaches their per-agent services."""

    agents: tuple[AgentReference, ...] = Field(default=(), description="Agents chosen by id.")
    groups: tuple[GroupReference, ...] = Field(
        default=(),
        description="Groups of agents (agent_groups:), each choosing all of its agents.",
    )

    @model_validator(mode="after")
    def _check_not_empty(self) -> Self:
        if not (self.agents or self.groups):
            raise ValueError("an agent selection names at least one agent or group")
        return self


def _read_grantee_kind(grantee: object) -> str:
    """Which member of ``Grantee`` a raw grant is: a mapping chooses agents, anything else names a service."""
    return "agents" if isinstance(grantee, Mapping | AgentSelection) else "service"


# Who a grant goes to: a service by config name (a per-agent service: every agent's copy), or chosen agents.
type Grantee = Annotated[
    Annotated[ServiceReference, Tag("service")] | Annotated[AgentSelection, Tag("agents")],
    Discriminator(_read_grantee_kind),
]


class NetworkDeclaration(TopologyEntry):
    """One ``networks:`` entry. A service joins a network it lists; chosen agents' sandboxes may join too."""

    internal: bool = Field(
        default=True,
        description="Whether the network has no route out of the host: every network but the core's egress.",
    )
    per_agent: bool = Field(
        default=False,
        description=(
            "Rendered once per agent, as <name>-<agent id>: an agent's sandbox joins its own copy only, and "
            "any other service listing it (the edge, the runner) joins every copy, so the sandboxes share no "
            "network yet each still reaches the edge and is reached by the runner."
        ),
    )
    sandboxes: AgentSelection | None = Field(
        default=None,
        description="Chosen agents whose sandboxes join it, besides the services that list it themselves.",
    )


class VolumeDeclaration(TopologyEntry):
    """One ``volumes:`` entry: a named volume, who writes it and who reads it.

    The stack's own volumes (``STACK_VOLUME_MOUNT_PATHS``) are mounted where its code expects them. Any other
    is an extra volume: memory-backed (a tmpfs of ``size_bytes``) and owned by its writers' user, because
    Docker creates a volume at a path the images lack as root, where the non-root services cannot write.
    """

    read_write: tuple[Grantee, ...] = Field(
        default=(),
        description="Who writes it: services or chosen agents.",
    )
    read_only: tuple[Grantee, ...] = Field(
        default=(),
        description="Who reads it, mounted read-only: services or chosen agents.",
    )
    per_agent: bool = Field(
        default=False,
        description=(
            "Rendered once per agent, as <name>-<agent id>: an agent's sandbox mounts its own copy at "
            "mount_path, and any other service granted it mounts every copy, at <mount_path>/<agent id>."
        ),
    )
    mount_path: PurePosixPath | None = Field(
        default=None,
        description="Where an extra volume is mounted; the stack's own volumes take their fixed path.",
    )
    size_bytes: PositiveInt | None = Field(
        default=None,
        description="The size of an extra volume's tmpfs; its pages count against the writer's mem_limit.",
    )
    recorded_in_mirror: bool = Field(
        default=False,
        description=(
            "Whether the monitors' mirror log records what is written to it. Only false is supported: "
            "the mirror records each tool call with its arguments and result (loc_arena/scaffold/tools.py), "
            "never a volume's content, so a write by a process an agent left running is not seen."
        ),
    )

    @field_validator("mount_path")
    @classmethod
    def _check_absolute(cls, mount_path: PurePosixPath | None) -> PurePosixPath | None:
        if mount_path is not None and not mount_path.is_absolute():
            raise ValueError(f"a mount path is absolute, got {mount_path}")
        return mount_path

    @field_validator("recorded_in_mirror")
    @classmethod
    def _check_not_recorded(cls, recorded: bool) -> bool:
        if recorded:
            raise ValueError(
                "recording a volume in the mirror is not built: it needs a reader of the volume that writes "
                "each change as a mirror event, e.g. the runner diffing it after every tool call and "
                "recording the diff through its Recorder (loc_arena/scaffold/bus.py)",
            )
        return recorded


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
    volumes: dict[str, VolumeDeclaration] = Field(description="The stack's named volumes.")
    secrets: dict[str, SecretDeclaration] = Field(
        default_factory=dict,
        description="The stack's secrets, readable under /run/secrets by the services granted them.",
    )
    agent_groups: dict[str, Annotated[tuple[AgentReference, ...], Field(min_length=1)]] = Field(
        default_factory=dict,
        description="Named groups of the run's agents, which network and volume grants may choose by name.",
    )

    @field_validator("volumes")
    @classmethod
    def _check_mount_paths(cls, volumes: dict[str, VolumeDeclaration]) -> dict[str, VolumeDeclaration]:
        """The stack's own volumes keep their fixed path; an extra one declares its path and tmpfs size."""
        for name, volume in volumes.items():
            declares_storage = volume.mount_path is not None or volume.size_bytes is not None
            if name in STACK_VOLUME_MOUNT_PATHS and declares_storage:
                raise ValueError(
                    f"volumes.{name} is mounted at {STACK_VOLUME_MOUNT_PATHS[name]}, where its services' "
                    "code expects it: it takes no mount_path or size_bytes",
                )
            if name not in STACK_VOLUME_MOUNT_PATHS and (
                volume.mount_path is None or volume.size_bytes is None
            ):
                raise ValueError(
                    f"volumes.{name} is an extra volume (not one of {sorted(STACK_VOLUME_MOUNT_PATHS)}): it "
                    "needs a mount_path and a size_bytes",
                )
        return volumes

    @model_validator(mode="after")
    def _check_agent_copy_names(self, info: ValidationInfo) -> Self:
        """No agent's copy of a per-agent network or volume takes the name of a declared one."""
        agents = _read_declared_names(info).agents
        for block, entries in (("networks", self.networks), ("volumes", self.volumes)):
            for name, entry in entries.items():
                clashes = {build_agent_copy_name(name, agent) for agent in agents} & set(entries)
                if entry.per_agent and clashes:
                    raise ValueError(
                        f"{block}.{name} is per agent, and its copies clash with {sorted(clashes)}",
                    )
        return self

    def select_agents(self, selection: AgentSelection) -> frozenset[str]:
        """The ids of the agents ``selection`` chooses, by id or through its groups."""
        return frozenset(selection.agents).union(*(self.agent_groups[group] for group in selection.groups))

    def mount_path(self, volume_name: str) -> PurePosixPath:
        """Where volume ``volume_name`` is mounted (for a per-agent volume: an agent's own copy)."""
        declared = self.volumes[volume_name].mount_path
        return declared if declared is not None else PurePosixPath(STACK_VOLUME_MOUNT_PATHS[volume_name])


def _declared_keys(raw: Mapping[str, object], block: str) -> frozenset[str]:
    """The names declared under ``block`` of ``raw``: none when it is no mapping (the model then says why)."""
    entries = raw.get(block)
    return frozenset(str(name) for name in entries) if isinstance(entries, Mapping) else frozenset()


def load_stack_topology(raw: Mapping[str, object], agent_ids: Collection[str]) -> StackTopology:
    """Validate the topology blocks of the resolved run config ``raw``; other keys are left to their owners.

    ``agent_ids`` are the run's agents, which grants and groups may name. Raises pydantic's
    ``ValidationError`` (a ``ValueError``) naming the path of every bad entry.
    """
    declared = DeclaredNames(
        services=_declared_keys(raw, "services"),
        networks=_declared_keys(raw, "networks"),
        secrets=_declared_keys(raw, "secrets"),
        agents=frozenset(agent_ids),
        groups=_declared_keys(raw, "agent_groups"),
    )
    blocks = {name: raw[name] for name in StackTopology.model_fields if name in raw}
    return StackTopology.model_validate(blocks, context=declared)
