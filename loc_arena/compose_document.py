"""The shape of a rendered compose file: the keys ``loc_arena.episode_stack`` writes."""

from __future__ import annotations

from typing import TYPE_CHECKING, NotRequired, TypedDict

if TYPE_CHECKING:
    from collections.abc import Sequence

    from loc_arena.settings import StackSettings


class Healthcheck(TypedDict):
    """A compose healthcheck."""

    test: list[str]
    interval: str  # between probes once the container is healthy, or its start period is over
    retries: int
    start_period: str  # a failed probe counts only once it is over
    start_interval: str  # between probes inside the start period, until one passes


def render_healthcheck(test: Sequence[str], stack: StackSettings) -> Healthcheck:
    """A healthcheck running ``test``: often until the container answers, seldom once it has.

    Docker probes at the start interval only inside the start period, so the period holds the container's
    start and its retries: it has as long to answer as when every probe came at one interval. A probe enters
    the container, which costs the machine far more than the probe's own work, so a settled container is
    probed at ``gateway_health_settled_seconds``.
    """
    often = stack.gateway_health_interval_seconds
    return {
        "test": list(test),
        "interval": f"{stack.gateway_health_settled_seconds}s",
        "retries": stack.gateway_health_settled_retries,
        "start_period": f"{stack.gateway_health_start_seconds + stack.gateway_health_retries * often}s",
        "start_interval": f"{often}s",
    }


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
    tmpfs: list[str]  # each a path in the container, and its options: a memory filesystem there


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
