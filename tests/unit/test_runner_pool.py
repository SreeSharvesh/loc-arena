"""A stack run's runner pool: ``runner-1`` to ``runner-<n>``, sandboxes only the pool's owner drives.

Read from the compose file a stack run renders, for the aurora run with its agents service given two runners.
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import pytest
import yaml
from loc_arena.compose_document import ComposeDocument, ServiceSecret
from loc_arena.config import RunConfig, load_run_config
from loc_arena.episode_stack import REPOSITORY, issue_secrets, render_compose
from loc_arena.gateway.model_gateway import (
    EPISODE_TOKEN_VARIABLE,
    HOST_TOKEN_VARIABLE,
    MODEL_GATEWAY_CONFIG_VARIABLE,
)
from loc_arena.runner_pool import runner_token_variable
from sandbox_server.server import ServerSettings

RUN = "aurora-efficiency"
AURORA = load_run_config(REPOSITORY / "configs" / f"{RUN}.yaml")
OWNER = "agents"
HEALTHY = {"condition": "service_healthy"}


def hash_of(token: str) -> str:
    return f"sha256:{hashlib.sha256(token.encode()).hexdigest()}"


def with_runners(config: RunConfig, count: int) -> RunConfig:
    services = tuple(
        dataclasses.replace(service, runners=count if service.name == OWNER else 0)
        for service in config.live_services
    )
    return dataclasses.replace(config, live_services=services)


def secret_sources(secrets: list[str | ServiceSecret]) -> set[str]:
    return {secret if isinstance(secret, str) else secret["source"] for secret in secrets}


@pytest.fixture
def compose() -> ComposeDocument:
    return render_compose(with_runners(AURORA, 2), REPOSITORY, RUN, [])


def test_each_runner_runs_the_runner_image_on_agent_net_alone_with_no_capability_or_port(
    compose: ComposeDocument,
) -> None:
    runners = [compose["services"][runner] for runner in ("runner-1", "runner-2")]

    shape = {(runner["image"], tuple(runner["networks"]), tuple(runner["cap_drop"])) for runner in runners}
    assert (shape, [runner.get("ports") for runner in runners]) == (
        {("loc-arena-runner:latest", ("agent-net",), ("ALL",))},
        [None, None],
    )


def test_only_the_first_runner_builds_the_image_the_others_run(compose: ComposeDocument) -> None:
    runners = [compose["services"][runner] for runner in ("runner-1", "runner-2")]

    built = [("build" in runner, runner.get("pull_policy")) for runner in runners]
    assert built == [(True, None), (False, "never")]


def test_a_runners_command_server_serves_the_pools_owner_in_an_empty_workspace(
    compose: ComposeDocument,
) -> None:
    runner = compose["services"]["runner-1"]

    settings = ServerSettings.model_validate_json(runner["command"][-1])

    served = (settings.trusted_caller, settings.checkouts_directory, settings.command_timeout_limit_seconds)
    assert served == (OWNER, Path("/workspace"), 600.0)


def test_a_runner_reaches_the_gateway_on_the_runners_route_with_its_own_token_and_phoenix(
    compose: ComposeDocument,
) -> None:
    environment = compose["services"]["runner-1"]["environment"]

    assert environment == {
        "LOC_ARENA_GATEWAY_API": "http://gateway:8080/runner/api/v1",
        "LOC_ARENA_GATEWAY_TOKEN_FILE": "/run/secrets/sandbox_token",
        "PHOENIX_COLLECTOR_ENDPOINT": "http://phoenix:6006",
    }


def test_a_runners_token_is_mounted_in_that_runner_and_the_pools_owner_alone(
    compose: ComposeDocument,
) -> None:
    holders = {
        name
        for name, service in compose["services"].items()
        if "runner_token_runner_1" in secret_sources(service.get("secrets", []))
    }

    assert holders == {"runner-1", OWNER}


def test_the_pools_owner_starts_once_every_runner_is_healthy(compose: ComposeDocument) -> None:
    depends_on = compose["services"][OWNER]["depends_on"]
    assert depends_on == {"iam": HEALTHY, "runner-1": HEALTHY, "runner-2": HEALTHY}


def test_a_run_whose_scenario_has_no_runners_renders_none() -> None:
    services = render_compose(with_runners(AURORA, 0), REPOSITORY, RUN, [])["services"]

    assert [name for name in services if name.startswith("runner-")] == []


def test_each_plays_model_gateway_admits_its_runners_and_the_episode_and_host_of_the_whole_run() -> None:
    plays = issue_secrets(with_runners(AURORA, 2))

    admitted = [
        {
            key["metadata"]["caller"]: key["keyHash"]
            for route in yaml.safe_load(environment[MODEL_GATEWAY_CONFIG_VARIABLE])["binds"][0]["listeners"][
                0
            ]["routes"]
            for key in route["policies"]["apiKey"]["keys"]
            if key["metadata"]["caller"] in ("episode", "host", "runner-1", "runner-2")
        }
        for environment in plays
    ]

    expected = [
        {
            "episode": hash_of(environment[EPISODE_TOKEN_VARIABLE]),
            "host": hash_of(environment[HOST_TOKEN_VARIABLE]),
            "runner-1": hash_of(environment[runner_token_variable("runner-1")]),
            "runner-2": hash_of(environment[runner_token_variable("runner-2")]),
        }
        for environment in plays
    ]
    assert (admitted, admitted[0]["episode"] == admitted[1]["episode"]) == (expected, True)
