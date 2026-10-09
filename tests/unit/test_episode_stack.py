"""The rendered compose file: the key and the route out in the gateway alone, the logs out of the sandbox."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.episode_stack import (
    AGENT_NETWORK,
    EGRESS_NETWORK,
    ComposeDocument,
    render_compose,
    sandbox_agent_code,
)
from loc_arena.gateway import core
from loc_arena.settings import GatewaySettings, LocArenaSettings, StackSettings
from loc_arena.task import SANDBOX_URL_VARIABLE
from sandbox_server.server import ServerSettings
from scenarios.loader import SCENARIOS_ROOT

REPOSITORY = Path("/repository")
SCRIPTED_CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")


def render(settings: LocArenaSettings | None = None, *, policy: str = "scripted") -> ComposeDocument:
    config = dataclasses.replace(SCRIPTED_CONFIG, settings=settings or LocArenaSettings(), policy=policy)
    return render_compose(config, REPOSITORY, "a-run", ["--mode", "honest"])


def scenario_mounts(compose: ComposeDocument) -> dict[str, list[str]]:
    return {
        name: [volume for volume in service.get("volumes", []) if "scenarios" in volume]
        for name, service in compose["services"].items()
    }


def test_only_the_gateway_is_on_the_network_with_a_route_out() -> None:
    compose = render()

    on_egress = [
        name for name, service in compose["services"].items() if EGRESS_NETWORK in service["networks"]
    ]

    assert (on_egress, compose["networks"][EGRESS_NETWORK]["internal"]) == (["gateway"], False)


@pytest.mark.parametrize("service", ["episode", "sandbox"])
def test_the_episode_and_its_sandbox_are_only_on_the_network_with_no_route_out(service: str) -> None:
    compose = render()

    networks = compose["services"][service]["networks"]

    assert (networks, compose["networks"][AGENT_NETWORK]["internal"]) == ([AGENT_NETWORK], True)


def test_each_service_mounts_only_the_volumes_it_needs() -> None:
    compose = render(policy="model")

    sources = {
        name: sorted(volume.split(":")[0] for volume in service["volumes"])
        for name, service in compose["services"].items()
    }

    assert sources == {
        "gateway": ["/repository/configs", "sealed"],
        "sandbox": ["checkouts"],
        "episode": ["/repository/configs", "checkouts", "output"],
    }


def test_each_secret_goes_only_to_the_services_that_need_it() -> None:
    compose = render()

    holders = {name: service.get("secrets") for name, service in compose["services"].items()}

    assert (holders, compose["secrets"]) == (
        {"gateway": ["openrouter_api_key"], "sandbox": ["sandbox_token"], "episode": ["sandbox_token"]},
        {
            "openrouter_api_key": {"environment": "OPENROUTER_API_KEY"},
            "sandbox_token": {"environment": "LOC_ARENA_SANDBOX_TOKEN"},
        },
    )


def test_every_service_drops_every_capability() -> None:
    compose = render()

    dropped = {name: service["cap_drop"] for name, service in compose["services"].items()}

    assert dropped == {"gateway": ["ALL"], "sandbox": ["ALL"], "episode": ["ALL"]}


def test_the_episode_sends_its_model_calls_to_the_gateway_and_its_code_to_the_sandbox() -> None:
    settings = LocArenaSettings(gateway=GatewaySettings(port=9191), stack=StackSettings(sandbox_port=9292))

    environment = render(settings)["services"]["episode"]["environment"]

    assert environment == {
        core.GATEWAY_URL_VARIABLE: "http://gateway:9191/api/v1/chat/completions",
        SANDBOX_URL_VARIABLE: "http://sandbox:9292",
    }


def test_the_sandbox_gets_the_settings_its_command_server_needs_in_its_command() -> None:
    settings = LocArenaSettings(
        gateway=GatewaySettings(secrets_dir=Path("/the/secrets")),
        stack=StackSettings(
            sandbox_port=9292,
            checkouts_directory=Path("/the/checkouts"),
            sandbox_scratch_directories=(Path("/the/home"), Path("/the/tmp")),
            command_output_limit_bytes=77,
        ),
    )

    command = render(settings)["services"]["sandbox"]["command"]

    assert (command[:3], ServerSettings.model_validate_json(command[-1])) == (
        ["python", "-m", "sandbox_server"],
        ServerSettings(
            port=9292,
            checkouts_directory=Path("/the/checkouts"),
            scratch_directories=(Path("/the/home"), Path("/the/tmp")),
            output_limit_bytes=77,
            secrets_dir=Path("/the/secrets"),
        ),
    )


def test_the_gateway_shares_the_episodes_network() -> None:
    compose = render()

    gateway_networks = compose["services"]["gateway"]["networks"]

    assert AGENT_NETWORK in gateway_networks


def test_the_gateway_reads_the_settings_of_the_requested_run() -> None:
    compose = render()

    command = compose["services"]["gateway"]["command"]

    assert command[-1] == "/app/configs/a-run.yaml"


def test_the_episode_runs_the_requested_run_and_mode() -> None:
    compose = render()

    command = compose["services"]["episode"]["command"]

    assert command[3:] == ["run", "--run", "a-run", "--mode", "honest", "--out", "/output"]


@pytest.mark.parametrize("service", ["episode", "sandbox"])
def test_the_image_and_limits_of_the_episode_and_the_sandbox_come_from_the_settings(service: str) -> None:
    stack = StackSettings(
        image="other:tag",
        episode_cpus=0.5,
        episode_memory_limit="1g",
        episode_pids_limit=64,
    )

    rendered = render(LocArenaSettings(stack=stack))["services"][service]

    limits = (rendered["image"], rendered["cpus"], rendered["mem_limit"], rendered["pids_limit"])
    assert limits == ("other:tag", 0.5, "1g", 64)


def test_the_gateway_port_is_published_on_the_hosts_loopback_alone() -> None:
    settings = LocArenaSettings(gateway=GatewaySettings(port=9191))

    published = {name: service.get("ports") for name, service in render(settings)["services"].items()}

    assert published == {"gateway": ["127.0.0.1::9191"], "sandbox": None, "episode": None}


def test_a_scripted_episode_gets_the_scripted_moves_mounted_read_only() -> None:
    compose = render(policy="scripted")

    mounts = scenario_mounts(compose)

    source = SCENARIOS_ROOT / "aurora_efficiency" / "scripted"
    assert mounts == {
        "gateway": [],
        "sandbox": [],
        "episode": [f"{source}:/app/scenarios/aurora_efficiency/scripted:ro"],
    }


def test_a_live_model_episode_gets_nothing_of_the_scenario_mounted() -> None:
    compose = render(policy="model")

    mounts = scenario_mounts(compose)

    assert mounts == {"gateway": [], "sandbox": [], "episode": []}


def test_grading_a_stack_run_sandboxes_the_agent_code_its_config_runs_in_process() -> None:
    config = load_run_config("configs/aurora-efficiency.deterministic.yaml")
    assert not config.settings.stack.sandbox_agent_code

    graded_with = sandbox_agent_code(config)

    assert graded_with.settings.stack.sandbox_agent_code


def test_a_sandbox_its_agents_killed_comes_back() -> None:
    compose = render()

    restart = compose["services"]["sandbox"]["restart"]

    assert restart == "unless-stopped"
