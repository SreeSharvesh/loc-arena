"""The Compose Specification fields the harness renders, as the TypedDicts of the YAML it writes."""

from __future__ import annotations

from typing import NotRequired, TypedDict


class ComposeBuild(TypedDict):
    """A service's ``build``: the context, the stage of a multi-stage Dockerfile, and its build arguments."""

    context: str
    target: NotRequired[str]
    args: NotRequired[dict[str, str]]


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
