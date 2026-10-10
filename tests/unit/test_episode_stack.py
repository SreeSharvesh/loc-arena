"""The rendered compose file: the key and the route out in the gateway alone, the logs out of any sandbox.

The live services' credentials only in those services and in the sandboxes of the agents holding them.
"""

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
from scenarios.loader import SCENARIOS_ROOT, LiveService, load_scenario

REPOSITORY = Path("/repository")
SCRIPTED_CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
TWO_AGENTS = (SCRIPTED_CONFIG.agent("agent-main"), SCRIPTED_CONFIG.agent("serving-agent"))
SANDBOXES = ["sandbox-agent-main", "sandbox-serving-agent"]
SANDBOXES_WITH_A_NON_HOLDER = ["sandbox-agent-main", "sandbox-controlplane-agent"]
RUN_CONFIG_SERVICES = [*SANDBOXES_WITH_A_NON_HOLDER, "episode", "gateway"]
HEALTHY = {"condition": "service_healthy"}
# A holder of the notes credential and an agent holding none, as the run config grants them.
HOLDER_AND_NOT = (SCRIPTED_CONFIG.agent("agent-main"), SCRIPTED_CONFIG.agent("controlplane-agent"))
LIVE_SERVICES = ["notes", "wiki", "board"]
PACK = """\
scorer: aurora_pipeline_cost
verifier: kill_chain_v1
services:
  notes: {build: services/notes, port: 8000, accepts: [notes], description: the notes board}
  wiki:
    image: example/wiki:1
    command: [wiki, serve]
    port: 9000
    healthcheck: [CMD, wiki, ping]
    accepts: [wiki, wiki-admin]
  board: {build: services/board, port: 7000}
  ticketboard: {port: 8090}
"""


def render(settings: LocArenaSettings | None = None, *, policy: str = "scripted") -> ComposeDocument:
    config = dataclasses.replace(
        SCRIPTED_CONFIG,
        settings=settings or LocArenaSettings(),
        policy=policy,
        agents=TWO_AGENTS,
        live_services=(),
    )
    return render_compose(config, REPOSITORY, "a-run", ["--mode", "honest"])


@pytest.fixture
def pack(tmp_path: Path) -> Path:
    directory = tmp_path / "pack"
    for built in ("notes", "board"):
        (directory / "services" / built).mkdir(parents=True)
        (directory / "services" / built / "Dockerfile").write_text("FROM scratch\n")
    (directory / "scenario.yaml").write_text(PACK)
    return directory.resolve()


@pytest.fixture
def live_services(pack: Path) -> tuple[LiveService, ...]:
    return load_scenario(pack.name, root=pack.parent).live_services


def render_live(
    services: tuple[LiveService, ...],
    settings: LocArenaSettings | None = None,
) -> ComposeDocument:
    config = dataclasses.replace(
        SCRIPTED_CONFIG,
        settings=settings or LocArenaSettings(),
        agents=HOLDER_AND_NOT,
        live_services=services,
    )
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


def test_the_stack_has_one_sandbox_per_agent() -> None:
    compose = render()

    services = sorted(compose["services"])

    assert services == ["episode", "gateway", *SANDBOXES]


@pytest.mark.parametrize("service", ["episode", *SANDBOXES])
def test_the_episode_and_every_sandbox_are_only_on_the_network_with_no_route_out(service: str) -> None:
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
        "sandbox-agent-main": ["checkouts"],
        "sandbox-serving-agent": ["checkouts"],
        "episode": ["/repository/configs", "checkouts", "output"],
    }


def test_each_sandbox_mounts_only_its_own_agents_token_as_sandbox_token() -> None:
    compose = render()

    mounted = {name: compose["services"][name]["secrets"] for name in SANDBOXES}

    assert mounted == {
        "sandbox-agent-main": [{"source": "sandbox_token_agent_main", "target": "sandbox_token"}],
        "sandbox-serving-agent": [{"source": "sandbox_token_serving_agent", "target": "sandbox_token"}],
    }


def test_the_gateway_holds_the_key_and_the_episode_every_sandbox_token() -> None:
    compose = render()

    holders = {name: compose["services"][name]["secrets"] for name in ("gateway", "episode")}

    assert holders == {
        "gateway": ["openrouter_api_key"],
        "episode": ["sandbox_token_agent_main", "sandbox_token_serving_agent"],
    }


def test_compose_reads_each_secret_from_its_own_host_variable() -> None:
    compose = render()

    sources = compose["secrets"]

    assert sources == {
        "openrouter_api_key": {"environment": "OPENROUTER_API_KEY"},
        "sandbox_token_agent_main": {"environment": "LOC_ARENA_SANDBOX_TOKEN_AGENT_MAIN"},
        "sandbox_token_serving_agent": {"environment": "LOC_ARENA_SANDBOX_TOKEN_SERVING_AGENT"},
    }


def test_every_service_drops_every_capability() -> None:
    compose = render()

    dropped = {name: service["cap_drop"] for name, service in compose["services"].items()}

    assert dropped == {
        "gateway": ["ALL"],
        "sandbox-agent-main": ["ALL"],
        "sandbox-serving-agent": ["ALL"],
        "episode": ["ALL"],
    }


def test_the_episode_sends_its_model_calls_to_the_gateway_and_each_agents_code_to_its_sandbox() -> None:
    settings = LocArenaSettings(gateway=GatewaySettings(port=9191), stack=StackSettings(sandbox_port=9292))

    environment = render(settings)["services"]["episode"]["environment"]

    assert environment == {
        core.GATEWAY_URL_VARIABLE: "http://gateway:9191/api/v1/chat/completions",
        SANDBOX_URL_VARIABLE: "http://sandbox-{agent}:9292",
    }


def test_the_episode_starts_once_the_gateway_and_every_sandbox_are_healthy() -> None:
    compose = render()

    awaited = compose["services"]["episode"]["depends_on"]

    assert awaited == {"gateway": HEALTHY, "sandbox-agent-main": HEALTHY, "sandbox-serving-agent": HEALTHY}


@pytest.mark.parametrize("sandbox", SANDBOXES)
def test_every_sandbox_gets_the_settings_its_command_server_needs_in_its_command(sandbox: str) -> None:
    settings = LocArenaSettings(
        gateway=GatewaySettings(secrets_dir=Path("/the/secrets")),
        stack=StackSettings(
            sandbox_port=9292,
            checkouts_directory=Path("/the/checkouts"),
            sandbox_scratch_directories=(Path("/the/home"), Path("/the/tmp")),
            command_output_limit_bytes=77,
            shell_timeout_seconds=40,
            run_tests_timeout_seconds=50,
            run_benchmark_timeout_seconds=30,
        ),
    )

    command = render(settings)["services"][sandbox]["command"]

    assert (command[:3], ServerSettings.model_validate_json(command[-1])) == (
        ["python", "-m", "sandbox_server"],
        ServerSettings(
            port=9292,
            checkouts_directory=Path("/the/checkouts"),
            scratch_directories=(Path("/the/home"), Path("/the/tmp")),
            output_limit_bytes=77,
            secrets_dir=Path("/the/secrets"),
            trusted_caller="episode",
            command_timeout_limit_seconds=50,
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


def test_each_service_runs_its_targets_image_and_one_sandbox_builds_the_sandboxes() -> None:
    stack = StackSettings(image="engine:tag", sandbox_image="sandbox:tag")

    services = render(LocArenaSettings(stack=stack))["services"]

    built = {name: (service.get("build"), service["image"]) for name, service in services.items()}
    assert built == {
        "gateway": ({"context": "/repository", "target": "engine"}, "engine:tag"),
        "sandbox-agent-main": ({"context": "/repository", "target": "sandbox"}, "sandbox:tag"),
        "sandbox-serving-agent": (None, "sandbox:tag"),
        "episode": ({"context": "/repository", "target": "engine"}, "engine:tag"),
    }


def test_a_sandbox_that_does_not_build_the_image_never_pulls_it() -> None:
    services = render()["services"]

    policy = services["sandbox-serving-agent"].get("pull_policy")

    assert policy == "never"


@pytest.mark.parametrize(
    ("service", "expected"),
    [("episode", (0.5, "1g", 64)), *[(sandbox, (0.25, "512m", 32)) for sandbox in SANDBOXES]],
)
def test_the_limits_of_the_episode_and_of_every_sandbox_come_from_their_own_settings(
    service: str,
    expected: tuple[float, str, int],
) -> None:
    stack = StackSettings(
        episode_cpus=0.5,
        episode_memory_limit="1g",
        episode_pids_limit=64,
        sandbox_cpus=0.25,
        sandbox_memory_limit="512m",
        sandbox_pids_limit=32,
    )

    rendered = render(LocArenaSettings(stack=stack))["services"][service]

    limits = (rendered["cpus"], rendered["mem_limit"], rendered["pids_limit"])
    assert limits == expected


def test_the_gateway_port_is_published_on_the_hosts_loopback_alone() -> None:
    settings = LocArenaSettings(gateway=GatewaySettings(port=9191))

    published = {name: service.get("ports") for name, service in render(settings)["services"].items()}

    assert published == {
        "gateway": ["127.0.0.1::9191"],
        "sandbox-agent-main": None,
        "sandbox-serving-agent": None,
        "episode": None,
    }


def test_a_scripted_episode_gets_the_scripted_moves_mounted_read_only() -> None:
    compose = render(policy="scripted")

    mounts = scenario_mounts(compose)

    source = SCENARIOS_ROOT / "aurora_efficiency" / "scripted"
    assert mounts == {
        "gateway": [],
        "sandbox-agent-main": [],
        "sandbox-serving-agent": [],
        "episode": [f"{source}:/app/scenarios/aurora_efficiency/scripted:ro"],
    }


def test_a_live_model_episode_gets_nothing_of_the_scenario_mounted() -> None:
    compose = render(policy="model")

    mounts = scenario_mounts(compose)

    assert mounts == {"gateway": [], "sandbox-agent-main": [], "sandbox-serving-agent": [], "episode": []}


def test_grading_a_stack_run_sandboxes_the_agent_code_its_config_runs_in_process() -> None:
    config = load_run_config("configs/aurora-efficiency.deterministic.yaml")
    assert not config.settings.stack.sandbox_agent_code

    graded_with = sandbox_agent_code(config)

    assert graded_with.settings.stack.sandbox_agent_code


@pytest.mark.parametrize("sandbox", SANDBOXES)
def test_a_sandbox_its_agent_killed_comes_back(sandbox: str) -> None:
    compose = render()

    restart = compose["services"][sandbox]["restart"]

    assert restart == "unless-stopped"


def test_a_live_service_built_from_its_pack_runs_bounded_on_agent_net_alone_with_what_it_checks(
    pack: Path,
    live_services: tuple[LiveService, ...],
) -> None:
    stack = StackSettings(service_memory_limit="300m", service_cpus=0.5, service_pids_limit=64)

    notes = render_live(live_services, LocArenaSettings(stack=stack))["services"]["notes"]

    assert notes == {
        "build": {"context": f"{pack}/services/notes"},
        "image": "loc-arena-service-notes:latest",
        "cap_drop": ["ALL"],
        "mem_limit": "300m",
        "cpus": 0.5,
        "pids_limit": 64,
        "networks": ["agent-net"],
        "secrets": [{"source": "credential_notes", "target": "credential_notes"}],
        "healthcheck": {
            "test": [
                "CMD",
                "python",
                "-c",
                "import socket; socket.create_connection(('localhost', 8000), 2)",
            ],
            "interval": "2s",
            "retries": 15,
        },
        "init": True,
    }


def test_a_live_service_from_a_ready_image_runs_its_command_and_healthcheck(
    live_services: tuple[LiveService, ...],
) -> None:
    compose = render_live(live_services)

    wiki = compose["services"]["wiki"]

    assert (wiki["image"], wiki.get("build"), wiki["command"], wiki["healthcheck"]["test"]) == (
        "example/wiki:1",
        None,
        ["wiki", "serve"],
        ["CMD", "wiki", "ping"],
    )


def test_every_live_service_is_only_on_the_network_with_no_route_out(
    live_services: tuple[LiveService, ...],
) -> None:
    compose = render_live(live_services)

    networks = {name: compose["services"][name]["networks"] for name in LIVE_SERVICES}

    assert networks == {"notes": [AGENT_NETWORK], "wiki": [AGENT_NETWORK], "board": [AGENT_NETWORK]}


def test_each_sandbox_holds_only_the_credentials_its_agent_is_granted(
    live_services: tuple[LiveService, ...],
) -> None:
    compose = render_live(live_services)

    mounted = {name: compose["services"][name]["secrets"] for name in SANDBOXES_WITH_A_NON_HOLDER}

    assert mounted == {
        "sandbox-agent-main": [
            {"source": "sandbox_token_agent_main", "target": "sandbox_token"},
            {"source": "credential_notes", "target": "credential_notes"},
        ],
        "sandbox-controlplane-agent": [
            {"source": "sandbox_token_controlplane_agent", "target": "sandbox_token"},
        ],
    }


def test_neither_the_gateway_nor_the_episode_holds_a_credential(
    live_services: tuple[LiveService, ...],
) -> None:
    compose = render_live(live_services)

    holders = {name: compose["services"][name]["secrets"] for name in ("gateway", "episode")}

    assert holders == {
        "gateway": ["openrouter_api_key"],
        "episode": ["sandbox_token_agent_main", "sandbox_token_controlplane_agent"],
    }


def test_compose_reads_each_credential_from_its_own_host_variable(
    live_services: tuple[LiveService, ...],
) -> None:
    compose = render_live(live_services)

    credentials = {
        name: source for name, source in compose["secrets"].items() if name.startswith("credential_")
    }

    assert credentials == {
        "credential_notes": {"environment": "LOC_ARENA_CREDENTIAL_NOTES"},
        "credential_wiki": {"environment": "LOC_ARENA_CREDENTIAL_WIKI"},
        "credential_wiki-admin": {"environment": "LOC_ARENA_CREDENTIAL_WIKI_ADMIN"},
    }


def test_the_episode_starts_once_every_live_service_is_healthy(
    live_services: tuple[LiveService, ...],
) -> None:
    compose = render_live(live_services)

    awaited = compose["services"]["episode"]["depends_on"]

    assert awaited == {
        "gateway": HEALTHY,
        "sandbox-agent-main": HEALTHY,
        "sandbox-controlplane-agent": HEALTHY,
        "notes": HEALTHY,
        "wiki": HEALTHY,
        "board": HEALTHY,
    }


def test_the_run_config_gives_notes_its_container_and_its_credential_to_all_but_controlplane_agent() -> None:
    config = load_run_config("configs/aurora-efficiency.yaml")

    compose = render_compose(config, REPOSITORY, "aurora-efficiency", [])

    services = compose["services"]
    holds = {name: "credential_notes" in str(services[name]["secrets"]) for name in RUN_CONFIG_SERVICES}
    assert ("notes" in services, holds) == (
        True,
        {"sandbox-agent-main": True, "sandbox-controlplane-agent": False, "episode": False, "gateway": False},
    )
