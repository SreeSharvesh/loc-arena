"""The shape of a rendered compose file: the keys ``loc_arena.episode_stack`` writes."""

from __future__ import annotations

from typing import NotRequired, TypedDict


class Healthcheck(TypedDict):
    """A compose healthcheck."""

    test: list[str]
    interval: str
    retries: int
    start_period: str  # a failed probe counts only once it is over
    start_interval: str


class ComposeBuild(TypedDict):
    """A compose build: the Dockerfile's directory, the stage to build and the secrets its steps may mount."""

    context: str
    target: str
    secrets: NotRequired[list[str]]


class ServiceBuild(TypedDict, total=False):
    """A live service's compose build: the directory of its Dockerfile, built whole."""

    context: str
    dockerfile: str
    additional_contexts: dict[str, str]
    secrets: list[str]


class ServiceSecret(TypedDict):
    """A compose secret mounted in a service under another file name: ``target`` in its secrets directory."""

    source: str
    target: str


class ComposeService(TypedDict, total=False):
    """The compose keys a rendered service uses."""

    build: ComposeBuild | ServiceBuild
    image: str
    pull_policy: str
    entrypoint: list[str]
    command: list[str]
    working_dir: str
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
    user: str
    stop_grace_period: str


class ComposeNetwork(TypedDict, total=False):
    """The compose keys a rendered network uses."""

    internal: bool
    driver_opts: dict[str, str]


class ComposeVolume(TypedDict, total=False):
    """The compose keys a rendered volume uses."""

    driver_opts: dict[str, str]


class ComposeDocument(TypedDict):
    """A rendered compose file."""

    services: dict[str, ComposeService]
    networks: dict[str, ComposeNetwork]
    volumes: dict[str, ComposeVolume]
    secrets: dict[str, dict[str, str]]
