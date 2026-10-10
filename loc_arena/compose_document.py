"""The shape of a rendered compose file: the keys ``loc_arena.episode_stack`` writes."""

from __future__ import annotations

from typing import TypedDict


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
