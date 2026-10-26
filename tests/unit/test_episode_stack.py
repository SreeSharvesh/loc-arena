"""The rendered compose file: the key and the route out in the gateway alone, the logs out of any sandbox.

Identities and starting rights sit in their sandbox and service alone; services' logs are copied out.
"""

from __future__ import annotations

import dataclasses
import socket
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import pytest
import yaml
from loc_arena import episode_stack
from loc_arena.config import AgentSandboxConfig, load_run_config
from loc_arena.episode_stack import (
    AGENT_NETWORK,
    ComposeDocument,
    Identity,
    StackError,
    issue_secrets,
    render_compose,
    run_in_stack,
    sandbox_agent_code,
)
from loc_arena.gateway import core
from loc_arena.harness import apply_mode
from loc_arena.observability import EXPORT_SPANS
from loc_arena.recording_service import COVERT_TOOL_NAMES
from loc_arena.settings import GatewaySettings, LocArenaSettings, StackSettings
from loc_arena.stack_play import renew_services
from loc_arena.task import SANDBOX_URL_VARIABLE, TOOLS_URL_VARIABLE
from loc_arena.tools_gateway import TOOLS_GATEWAY_CONFIG_VARIABLE, render_tools_gateway_config
from sandbox_server.server import ServerSettings
from scenarios.loader import SCENARIOS_ROOT, EngineModule, LiveService, load_scenario

REPOSITORY = Path("/repository")
COMPANY = ("common", "controlplane", "datapipe", "distill", "evalkit", "jobsvc", "serving")
COMPANY_PATH = ":".join(
    f"/app/company/meridian-{repo}" for repo in (*COMPANY, "iam")
)  # the engine image's company repos, and the IAM client
SCRIPTED_CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
TWO_AGENTS = (SCRIPTED_CONFIG.agent("agent-main"), SCRIPTED_CONFIG.agent("serving-agent"))
SANDBOXES = ["sandbox-agent-main", "sandbox-serving-agent"]
SANDBOXES_WITH_ONE_WITH_NO_RIGHT = ["sandbox-agent-main", "sandbox-controlplane-agent"]
HEALTHY = {"condition": "service_healthy"}
# The healthcheck of a live service on port 8000 that names no test of its own.
PORT_8000_PROBE = {
    "test": ["CMD", "python", "-c", "import socket; socket.create_connection(('localhost', 8000), 2)"],
    "interval": "2s",
    "retries": 15,
    "start_period": "60s",
    "start_interval": "2s",
}
SKILLS = SCENARIOS_ROOT / "aurora_efficiency" / "skills"
SKILLS_MOUNT = f"{SKILLS}:/skills:ro"  # read-only, where the default settings put them
# An agent with rights on notes and an agent with none, as the run config seeds them.
WITH_RIGHTS_AND_WITHOUT = (SCRIPTED_CONFIG.agent("agent-main"), SCRIPTED_CONFIG.agent("controlplane-agent"))
LIVE_SERVICES = ["notes", "wiki", "status"]
# A service with tools, run from the engine image: agent-main may call one of its tools, the other none.
FORGE = LiveService(
    name="forge",
    source=EngineModule("loc_arena.forge.service"),
    port=8000,
    command=None,
    healthcheck=None,
    rights=(),
    description="the forge",
    tools=("open_pr", "close_pr"),
)
WITH_TOOLS_AND_WITHOUT = (
    dataclasses.replace(
        SCRIPTED_CONFIG.agent("agent-main"),
        sandbox=AgentSandboxConfig(tools={"forge": ("open_pr",)}),
    ),
    dataclasses.replace(SCRIPTED_CONFIG.agent("controlplane-agent"), sandbox=AgentSandboxConfig()),
)
TOOLS_CONFIG = dataclasses.replace(SCRIPTED_CONFIG, agents=WITH_TOOLS_AND_WITHOUT, live_services=(FORGE,))
STARTED = {"condition": "service_started"}
PACK = """\
scorer: aurora_pipeline_cost
verifier: kill_chain_v1
services:
  notes: {build: services/notes, port: 8000, rights: [read, write], description: the notes board}
  wiki:
    image: example/wiki:1
    command: [wiki, serve]
    port: 9000
    healthcheck: [CMD, wiki, ping]
  status: {build: services/status, port: 7000}
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
    for built in ("notes", "status"):
        (directory / "services" / built).mkdir(parents=True)
        dockerfile = "FROM scratch\nCOPY --from=meridian-iam /meridian_iam ./meridian_iam\n"  # the IAM client
        (directory / "services" / built / "Dockerfile").write_text(dockerfile)
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
        agents=WITH_RIGHTS_AND_WITHOUT,
        live_services=services,
    )
    return render_compose(config, REPOSITORY, "a-run", ["--mode", "honest"])


def render_tools(settings: LocArenaSettings | None = None) -> ComposeDocument:
    config = dataclasses.replace(TOOLS_CONFIG, settings=settings or LocArenaSettings())
    return render_compose(config, REPOSITORY, "a-run", ["--mode", "honest"])


@pytest.fixture
def tools_gateway_config() -> dict[str, Any]:
    tokens = {"agent-main": "token-main", "controlplane-agent": "token-control"}
    identities = {
        Identity("forge", "agent-main"): "id-main",
        Identity("forge", "controlplane-agent"): "id-control",
    }
    return yaml.safe_load(render_tools_gateway_config(TOOLS_CONFIG, tokens, identities))


def read_route(document: dict[str, Any]) -> dict[str, Any]:
    (bind,) = document["binds"]
    ((route,),) = [listener["routes"] for listener in bind["listeners"]]
    return route


def scenario_mounts(compose: ComposeDocument) -> dict[str, list[str]]:
    return {
        name: [volume for volume in service.get("volumes", []) if "scenarios" in volume]
        for name, service in compose["services"].items()
    }


def test_the_stack_has_one_sandbox_per_agent() -> None:
    compose = render()

    services = sorted(compose["services"])

    assert services == ["collector", "episode", "gateway", *SANDBOXES]


@pytest.mark.parametrize("service", ["episode", *SANDBOXES])
def test_the_episode_and_every_sandbox_are_only_on_the_network_with_no_route_out(service: str) -> None:
    compose = render()

    networks = compose["services"][service]["networks"]

    assert (networks, compose["networks"][AGENT_NETWORK]["internal"]) == ([AGENT_NETWORK], True)


def test_each_service_mounts_only_the_volumes_it_needs() -> None:
    compose = render(policy="model")

    sources = {
        name: sorted(volume.split(":")[0] for volume in service.get("volumes", []))
        for name, service in compose["services"].items()
    }

    assert sources == {
        "gateway": [],
        "collector": ["sealed"],
        "sandbox-agent-main": [str(SKILLS), "checkouts"],
        "sandbox-serving-agent": [str(SKILLS), "checkouts"],
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
        "gateway": ["openrouter_api_key", "model_gateway_config", "provider_roots"],
        "episode": ["gateway_token", "sandbox_token_agent_main", "sandbox_token_serving_agent"],
    }


def test_compose_reads_each_secret_from_its_own_host_variable() -> None:
    compose = render()

    sources = {name: source for name, source in compose["secrets"].items() if "file" not in source}

    assert sources == {
        "openrouter_api_key": {"environment": "OPENROUTER_API_KEY"},
        "model_gateway_config": {"environment": "LOC_ARENA_MODEL_GATEWAY_CONFIG"},
        "collector_config": {"environment": "LOC_ARENA_COLLECTOR_CONFIG"},
        "gateway_token": {"environment": "LOC_ARENA_GATEWAY_TOKEN"},
        "sandbox_token_agent_main": {"environment": "LOC_ARENA_SANDBOX_TOKEN_AGENT_MAIN"},
        "sandbox_token_serving_agent": {"environment": "LOC_ARENA_SANDBOX_TOKEN_SERVING_AGENT"},
    }


def test_every_service_drops_every_capability() -> None:
    compose = render()

    dropped = {name: service["cap_drop"] for name, service in compose["services"].items()}

    assert dropped == {
        "gateway": ["ALL"],
        "collector": ["ALL"],
        "sandbox-agent-main": ["ALL"],
        "sandbox-serving-agent": ["ALL"],
        "episode": ["ALL"],
    }


def test_the_episode_sends_its_model_calls_to_the_gateway_and_each_agents_code_to_its_sandbox() -> None:
    settings = LocArenaSettings(gateway=GatewaySettings(port=9191), stack=StackSettings(sandbox_port=9292))

    environment = render(settings)["services"]["episode"]["environment"]

    assert environment == {
        core.GATEWAY_URL_VARIABLE: "http://gateway:9191/api/v1/chat/completions",
        core.GATEWAY_TOKEN_FILE_VARIABLE: "/run/secrets/gateway_token",
        SANDBOX_URL_VARIABLE: "http://sandbox-{agent}:9292",
    }


def test_the_episode_starts_once_the_gateway_has_started_and_every_sandbox_is_healthy() -> None:
    compose = render()

    awaited = compose["services"]["episode"]["depends_on"]

    assert awaited == {"gateway": STARTED, "sandbox-agent-main": HEALTHY, "sandbox-serving-agent": HEALTHY}


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
            command_timeout_limit_seconds=40,
        ),
    )


def test_the_episode_runs_the_requested_run_and_mode() -> None:
    compose = render()

    command = compose["services"]["episode"]["command"]

    assert command[3:] == ["run", "--run", "a-run", "--mode", "honest", "--out", "/output"]


def test_each_service_runs_its_targets_image_and_one_sandbox_builds_the_sandboxes() -> None:
    stack = StackSettings(
        image="engine:tag",
        sandbox_image="sandbox:tag",
        tools_gateway_image="agentgateway:tag",
        collector_image="collector:tag",
    )

    services = render(LocArenaSettings(stack=stack))["services"]

    built = {name: (service.get("build"), service["image"]) for name, service in services.items()}
    assert built == {
        "gateway": (None, "agentgateway:tag"),
        "collector": (None, "collector:tag"),
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


def test_a_scripted_episode_gets_the_scripted_moves_mounted_read_only() -> None:
    compose = render(policy="scripted")

    mounts = scenario_mounts(compose)

    source = SCENARIOS_ROOT / "aurora_efficiency" / "scripted"
    assert mounts == {
        "gateway": [],
        "collector": [],
        "sandbox-agent-main": [SKILLS_MOUNT],
        "sandbox-serving-agent": [SKILLS_MOUNT],
        "episode": [f"{source}:/app/scenarios/aurora_efficiency/scripted:ro"],
    }


def test_a_live_model_episode_mounts_only_the_scenarios_skills_read_only_in_every_sandbox() -> None:
    compose = render(policy="model")

    mounts = scenario_mounts(compose)

    assert mounts == {
        "gateway": [],
        "collector": [],
        "sandbox-agent-main": [SKILLS_MOUNT],
        "sandbox-serving-agent": [SKILLS_MOUNT],
        "episode": [],
    }


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


def test_a_live_service_built_from_its_pack_runs_bounded_on_agent_net_alone_with_its_iam_credential(
    pack: Path,
    live_services: tuple[LiveService, ...],
) -> None:
    stack = StackSettings(service_memory_limit="300m", service_cpus=0.5, service_pids_limit=64)

    notes = render_live(live_services, LocArenaSettings(stack=stack))["services"]["notes"]

    assert notes == {
        "build": {
            "context": f"{pack}/services/notes",
            "additional_contexts": {"meridian-iam": str(Path(__file__).parents[2] / "company/meridian-iam")},
        },
        "image": "loc-arena-service-notes:latest",
        "cap_drop": ["ALL"],
        "mem_limit": "300m",
        "cpus": 0.5,
        "pids_limit": 64,
        "networks": ["agent-net"],
        "environment": {
            "LOC_ARENA_IAM_URL": "http://iam:8000",
            "LOC_ARENA_IAM_MANIFEST": "/srv/iam-manifest.json",
            "LOC_ARENA_IAM_CREDENTIAL": "/run/secrets/iam_credential",
            "LOC_ARENA_IAM_TIMEOUT_SECONDS": "2.0",
            "LOC_ARENA_IAM_RETRIES": "2",
        },
        "secrets": [{"source": "iam_service_notes", "target": "iam_credential"}],
        "depends_on": {"iam": HEALTHY},
        "healthcheck": PORT_8000_PROBE,
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


def test_a_live_service_with_no_rights_gets_no_identity_and_no_rights(
    live_services: tuple[LiveService, ...],
) -> None:
    compose = render_live(live_services)

    status = compose["services"]["status"]

    assert (status.get("secrets"), status.get("environment")) == (None, None)


def test_every_live_service_is_only_on_the_network_with_no_route_out(
    live_services: tuple[LiveService, ...],
) -> None:
    compose = render_live(live_services)

    networks = {name: compose["services"][name]["networks"] for name in LIVE_SERVICES}

    assert networks == {"notes": [AGENT_NETWORK], "wiki": [AGENT_NETWORK], "status": [AGENT_NETWORK]}


def test_each_sandbox_holds_exactly_its_own_identity_on_each_service_with_rights(
    live_services: tuple[LiveService, ...],
) -> None:
    compose = render_live(live_services)

    mounted = {name: compose["services"][name]["secrets"] for name in SANDBOXES_WITH_ONE_WITH_NO_RIGHT}

    assert mounted == {
        "sandbox-agent-main": [
            {"source": "sandbox_token_agent_main", "target": "sandbox_token"},
            {"source": "identity_notes_agent-main", "target": "identity_notes"},
        ],
        "sandbox-controlplane-agent": [
            {"source": "sandbox_token_controlplane_agent", "target": "sandbox_token"},
            {"source": "identity_notes_controlplane-agent", "target": "identity_notes"},
        ],
    }


def test_neither_the_gateway_nor_the_episode_holds_an_identity(
    live_services: tuple[LiveService, ...],
) -> None:
    compose = render_live(live_services)

    holders = {name: compose["services"][name]["secrets"] for name in ("gateway", "episode")}

    assert holders == {
        "gateway": ["openrouter_api_key", "model_gateway_config", "provider_roots"],
        "episode": ["gateway_token", "sandbox_token_agent_main", "sandbox_token_controlplane_agent"],
    }


def test_compose_reads_each_identity_from_its_own_host_variable(
    live_services: tuple[LiveService, ...],
) -> None:
    compose = render_live(live_services)

    identities = {name: source for name, source in compose["secrets"].items() if name.startswith("identity_")}

    assert identities == {
        "identity_notes_agent-main": {"environment": "LOC_ARENA_IDENTITY_NOTES_AGENT_MAIN"},
        "identity_notes_controlplane-agent": {"environment": "LOC_ARENA_IDENTITY_NOTES_CONTROLPLANE_AGENT"},
    }


def test_identities_that_would_share_one_host_variable_are_refused_naming_both(
    live_services: tuple[LiveService, ...],
) -> None:
    notes = next(service for service in live_services if service.name == "notes")
    config = dataclasses.replace(
        SCRIPTED_CONFIG,
        settings=LocArenaSettings(),
        agents=(
            SCRIPTED_CONFIG.agent("agent-main"),
            dataclasses.replace(SCRIPTED_CONFIG.agent("agent-main"), id="main"),
        ),
        live_services=(notes, dataclasses.replace(notes, name="notes-agent")),
    )

    with pytest.raises(StackError, match=r"\('notes', 'agent-main'\), \('notes-agent', 'main'\)"):
        render_compose(config, REPOSITORY, "a-run", [])


def test_the_episode_starts_once_every_live_service_is_healthy(
    live_services: tuple[LiveService, ...],
) -> None:
    compose = render_live(live_services)

    awaited = compose["services"]["episode"]["depends_on"]

    assert awaited == {
        "gateway": STARTED,
        "sandbox-agent-main": HEALTHY,
        "sandbox-controlplane-agent": HEALTHY,
        "notes": HEALTHY,
        "wiki": HEALTHY,
        "status": HEALTHY,
    }


def test_a_service_with_tools_runs_its_engine_module_with_the_run_config_and_its_iam_credential() -> None:
    stack = StackSettings(service_memory_limit="300m", service_cpus=0.5, service_pids_limit=64)

    forge = render_tools(LocArenaSettings(stack=stack))["services"]["forge"]

    assert forge == {
        "build": {"context": "/repository", "target": "engine"},
        "image": "loc-arena:latest",
        "cap_drop": ["ALL"],
        "command": ["python", "-m", "loc_arena.forge.service", "/app/configs/a-run.yaml", "forge"],
        "volumes": ["/repository/configs:/app/configs:ro"],
        "environment": {
            "PYTHONPATH": COMPANY_PATH,
            "LOC_ARENA_IAM_URL": "http://iam:8000",
            "LOC_ARENA_IAM_MANIFEST": "/app/scenarios/aurora_efficiency/services/iam-manifests/forge.json",
            "LOC_ARENA_IAM_CREDENTIAL": "/run/secrets/iam_credential",
            "LOC_ARENA_IAM_TIMEOUT_SECONDS": "2.0",
            "LOC_ARENA_IAM_RETRIES": "2",
        },
        "mem_limit": "300m",
        "cpus": 0.5,
        "pids_limit": 64,
        "networks": ["agent-net"],
        "secrets": [{"source": "iam_service_forge", "target": "iam_credential"}],
        "depends_on": {"iam": HEALTHY},
        "healthcheck": PORT_8000_PROBE,
        "init": True,
    }


def test_the_tools_gateway_runs_its_pinned_image_on_agent_net_alone_with_its_config_as_a_secret() -> None:
    stack = StackSettings(service_memory_limit="300m", service_cpus=0.5, service_pids_limit=64)

    compose = render_tools(LocArenaSettings(stack=stack))

    assert (compose["services"]["agentgateway"], compose["secrets"]["agentgateway_config"]) == (
        {
            "image": "ghcr.io/agentgateway/agentgateway:v1.6.0",
            "command": ["-f", "/run/secrets/agentgateway_config"],
            "secrets": ["agentgateway_config"],
            "cap_drop": ["ALL"],
            "mem_limit": "300m",
            "cpus": 0.5,
            "pids_limit": 64,
            "networks": ["agent-net"],
            "depends_on": {"forge": HEALTHY},
        },
        {"environment": "LOC_ARENA_AGENTGATEWAY_CONFIG"},
    )


def test_no_sandbox_holds_an_identity_on_a_service_with_tools() -> None:
    compose = render_tools()

    mounted = {name: compose["services"][name]["secrets"] for name in SANDBOXES_WITH_ONE_WITH_NO_RIGHT}

    assert mounted == {
        "sandbox-agent-main": [{"source": "sandbox_token_agent_main", "target": "sandbox_token"}],
        "sandbox-controlplane-agent": [
            {"source": "sandbox_token_controlplane_agent", "target": "sandbox_token"},
        ],
    }


def test_the_episode_reaches_the_tools_gateway_once_it_has_started() -> None:
    settings = LocArenaSettings(stack=StackSettings(tools_gateway_port=3333))

    episode = render_tools(settings)["services"]["episode"]

    assert (episode["environment"][TOOLS_URL_VARIABLE], episode["depends_on"]["agentgateway"]) == (
        "http://agentgateway:3333/mcp",
        STARTED,
    )


def test_the_tools_gateway_admits_each_agent_by_the_hash_of_its_sandbox_token_alone(
    tools_gateway_config: dict[str, Any],
) -> None:
    route = read_route(tools_gateway_config)

    admitted = route["policies"]["apiKey"]

    assert admitted == {
        "mode": "strict",
        "location": {"header": {"name": "authorization", "prefix": "Bearer "}},
        "keys": [
            {
                "keyHash": "sha256:9f0c4d86f324bac15104948c5eefb53b3b762d8a4a80246aa4fea61439c3edb3",
                "metadata": {"agent": "agent-main"},
            },
            {
                "keyHash": "sha256:739ef9718a71048ba90d9df45131c61651b0c0e3d49ab0d522ee30eeb2d20996",
                "metadata": {"agent": "controlplane-agent"},
            },
        ],
    }


def test_the_tools_gateway_allows_each_agent_exactly_the_tools_its_run_config_lists(
    tools_gateway_config: dict[str, Any],
) -> None:
    route = read_route(tools_gateway_config)

    rules = route["policies"]["mcpAuthorization"]

    assert rules == {
        "rules": [
            {
                "allow": 'mcp.tool.name in {"agent-main": {"forge": ["open_pr"]}, "controlplane-agent": {}}'
                "[apiKey.agent][mcp.tool.target]",
            },
        ],
    }


def test_the_tools_gateway_forwards_each_call_with_the_callers_identity_on_the_service(
    tools_gateway_config: dict[str, Any],
) -> None:
    route = read_route(tools_gateway_config)

    (backend,) = route["backends"]

    assert backend["mcp"]["targets"] == [
        {
            "name": "forge",
            "mcp": {"host": "http://forge:8000/mcp"},
            "policies": {
                "transformations": {
                    "request": {
                        "set": {
                            "authorization": '"Bearer " + {"agent-main": "id-main", '
                            '"controlplane-agent": "id-control"}[apiKey.agent]',
                        },
                    },
                },
            },
        },
    ]


def test_the_tools_gateway_offers_each_tool_by_its_own_name_with_no_service_prefix(
    tools_gateway_config: dict[str, Any],
) -> None:
    route = read_route(tools_gateway_config)

    (backend,) = route["backends"]

    assert backend["mcp"]["prefixMode"] == "never"


def test_the_tools_gateway_listens_on_its_port_alone_on_ipv4_with_no_admin_stats_or_readiness_listener(
    tools_gateway_config: dict[str, Any],
) -> None:
    names = ("enableIpv6", "adminAddr", "statsAddr", "readinessAddr")

    ports = [bind["port"] for bind in tools_gateway_config["binds"]]

    assert (ports, {name: tools_gateway_config["config"][name] for name in names}) == (
        [3000],
        {"enableIpv6": False, "adminAddr": "off", "statsAddr": "off", "readinessAddr": "off"},
    )


def test_the_tools_gateway_logs_each_request_as_json_naming_the_agent_and_a_tool_calls_arguments_and_outcome(
    tools_gateway_config: dict[str, Any],
) -> None:
    logged = (tools_gateway_config["config"]["logging"], tools_gateway_config["frontendPolicies"])

    assert logged == (
        {"format": "json"},
        {
            "accessLog": {
                "add": {
                    "agent": "apiKey.agent",
                    "tool_args": "mcp.tool.arguments",
                    "tool_result": "mcp.tool.result",
                    "tool_error": "mcp.tool.error",
                },
            },
        },
    )


COMPOSE_PREFIX_LENGTH = 8  # docker compose -p <project> -f <file> --project-directory <repository>
NOTES_LOG = b'{"caller": "agent-main", "status": 200}\n'
AURORA_AGENTS = ["AGENT_MAIN", "CONTROLPLANE_AGENT", "DATAPIPE_AGENT", "DISTILL_AGENT", "EVAL_AGENT"]
AURORA_AGENTS += ["JOBSVC_AGENT", "SERVING_AGENT"]
# The services agents hold an identity on, in the order a stack run's compose file declares them.
SERVICES = tuple("notes forge chat jobsvc jobs coverage agents evalkit tickets iam".split())
# The containers whose log a stack run copies, in that order: those, Phoenix, the fleet and the tools gateway.
LOGGED = (*SERVICES[:-1], "phoenix", "iam", "node-1", "node-2", "node-3", "agentgateway")


@dataclass
class FakeDocker:
    """``subprocess.run`` for the compose calls of a stack run whose last play exits 1: nothing is graded."""

    failing: str = "no subcommand"  # the compose subcommand or option whose call fails
    episode_created_only: bool = False  # a live service never became healthy, so the episode never started
    plays_passing: int = 0  # how many plays of the episode exit 0 before one exits 1
    unwritable_after_first_play: Path | None = None  # logs whose run's notes.log is a directory once played
    calls: list[list[str]] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    published: str = ""  # where the gateway's port is published: a socket the test listens on

    def __call__(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,  # none on the host's own call, for the gateway's published port
        stdout: IO[bytes] | None = None,
        text: bool = False,
        **_: object,
    ) -> subprocess.CompletedProcess[Any]:
        self.calls.append(command)
        self.environment = env or self.environment
        verb = command[COMPOSE_PREFIX_LENGTH]
        if verb == "logs" and stdout is not None:
            stdout.write(NOTES_LOG)
        playing = verb == "up" and "--attach" in command
        if playing and self.unwritable_after_first_play and self.plays == 1:
            (services,) = self.unwritable_after_first_play.glob("*/*/services")
            (services / "notes.log").mkdir()
        failed = self.failing in command[COMPOSE_PREFIX_LENGTH:] or (
            playing and self.plays > self.plays_passing
        )
        listed = verb == "ps" and (self.episode_created_only or "running" in command)  # Phoenix runs
        created = "container-id\n" if listed else ""
        published = self.published if verb == "port" else created
        printed = NOTES_LOG if verb == "logs" and stdout is None else published
        printed = printed.decode() if text and isinstance(printed, bytes) else printed
        printed = printed.encode() if not text and isinstance(printed, str) else printed  # as subprocess's
        return subprocess.CompletedProcess(command, int(failed), stdout=printed, stderr=printed[:0])

    @property
    def plays(self) -> int:
        return sum(call[COMPOSE_PREFIX_LENGTH] == "up" and "--attach" in call for call in self.calls)

    @property
    def verbs(self) -> list[str]:
        return [call[COMPOSE_PREFIX_LENGTH] for call in self.calls]


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDocker]:
    with socket.create_server(("127.0.0.1", 0)) as gateway:
        fake = FakeDocker(published=f"127.0.0.1:{gateway.getsockname()[1]}")
        monkeypatch.setattr(subprocess, "run", fake)
        yield fake


def run_aurora_in_stack(logs: Path, mode: str = "honest") -> None:
    run_in_stack("aurora-efficiency", mode=mode, seed=None, robust=False, logs=logs)


def read_copied_log(logs: Path, service: str) -> bytes:
    """The log of ``service`` a stack run copied into its run directory under ``logs``."""
    (copied,) = logs.glob(f"*/*/services/{service}.log")
    return copied.read_bytes()


def test_a_stack_run_hands_compose_a_distinct_identity_per_agent_and_service_through_its_environment_alone(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path)

    identities = {name: value for name, value in docker.environment.items() if "_IDENTITY_" in name}
    written = "".join(path.read_text() for path in (tmp_path / "compose").iterdir()) + str(docker.calls)
    assert (
        sorted(identities),
        len(set(identities.values())),
        [v for v in identities.values() if v in written],
    ) == (
        sorted(f"LOC_ARENA_IDENTITY_{s.upper()}_{a}" for s in SERVICES for a in AURORA_AGENTS),
        len(SERVICES) * len(AURORA_AGENTS),
        [],
    )


def test_a_stack_run_copies_each_live_services_log_out_before_it_removes_the_project(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path)

    copied = read_copied_log(tmp_path, "notes")
    commands = [call[COMPOSE_PREFIX_LENGTH:] for call in docker.calls]
    assert (copied, ["logs", "--no-color", "--no-log-prefix", "notes"] in commands, docker.verbs[-1]) == (
        NOTES_LOG,
        True,
        "down",
    )


@pytest.mark.parametrize(
    ("failing", "mode", "plays_passing"),
    [("notes", "honest", 0), ("python3.13", "honest", 0), ("python3.13", "attack", 1)],
    ids=["a-live-services-log", "phoenixs-spans-at-the-end-of-play", "phoenixs-spans-before-the-twin"],
)
def test_a_record_that_cannot_be_copied_out_keeps_the_project(
    docker: FakeDocker,
    tmp_path: Path,
    failing: str,
    mode: str,
    plays_passing: int,
) -> None:
    docker.failing = failing  # a live service's log, not the gateway's, or the export in a running Phoenix
    docker.plays_passing = plays_passing

    with pytest.raises(StackError, match="kept compose project"):
        run_aurora_in_stack(tmp_path, mode)

    assert (docker.verbs[-1], "down" in docker.verbs) == ("stop", False)


def test_a_stack_run_whose_episode_never_started_copies_each_live_services_log_before_it_removes_the_project(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.episode_created_only = True

    with pytest.raises(StackError, match="never started"):
        run_aurora_in_stack(tmp_path)

    copied = read_copied_log(tmp_path, "notes")
    commands = [call[COMPOSE_PREFIX_LENGTH:] for call in docker.calls]
    assert (copied, ["logs", "--no-color", "--no-log-prefix", "notes"] in commands, docker.verbs[-1]) == (
        NOTES_LOG,
        True,
        "down",
    )


def test_a_stack_run_whose_episode_never_started_says_a_live_service_may_have_failed_to_become_healthy(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.episode_created_only = True

    with pytest.raises(StackError) as raised:
        run_aurora_in_stack(tmp_path)

    (services,) = tmp_path.glob("*/*/services")
    assert "live service" in str(raised.value) and str(services) in str(raised.value)


def test_a_live_services_log_that_cannot_be_copied_keeps_the_project_though_the_episode_never_started(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.episode_created_only = True
    docker.failing = "logs"

    with pytest.raises(StackError, match="kept compose project"):
        run_aurora_in_stack(tmp_path)

    assert (docker.verbs[-1], "down" in docker.verbs) == ("stop", False)


def test_a_stack_run_hands_the_tools_gateway_its_config_through_its_environment_alone_without_a_token(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path)

    config = docker.environment["LOC_ARENA_AGENTGATEWAY_CONFIG"]
    tokens = [
        value for name, value in docker.environment.items() if name.startswith("LOC_ARENA_SANDBOX_TOKEN_")
    ]
    written = "".join(path.read_text() for path in (tmp_path / "compose").iterdir()) + str(docker.calls)
    assert (len(tokens), [token for token in tokens if token in config], config in written) == (7, [], False)


def test_a_stack_run_copies_the_tools_gateways_log_out_before_it_removes_the_project(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path)

    copied = read_copied_log(tmp_path, "agentgateway")
    copy_command = ["logs", "--no-color", "--no-log-prefix", "agentgateway"]
    commands = [call[COMPOSE_PREFIX_LENGTH:] for call in docker.calls]
    assert (copied, copy_command in commands, docker.verbs[-1]) == (NOTES_LOG, True, "down")


@pytest.fixture
def copied_at_grading(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The run directory's service logs as its events are built and as it is graded, in place of both."""
    seen: list[list[str]] = []

    def list_copied_logs(config: object, run_directory: Path, **_: object) -> Path:
        seen.append(sorted(path.name for path in (run_directory / "services").iterdir()))
        return run_directory

    monkeypatch.setattr(episode_stack, "build_recorded_run_events", list_copied_logs)
    monkeypatch.setattr(episode_stack, "grade_run", list_copied_logs)
    return seen


def test_a_stack_run_copies_each_live_services_log_into_its_run_directory_before_it_builds_and_grades(
    docker: FakeDocker,
    tmp_path: Path,
    copied_at_grading: list[list[str]],
) -> None:
    docker.plays_passing = 1

    run_aurora_in_stack(tmp_path)

    assert (
        copied_at_grading == [sorted([*(f"{service}.log" for service in LOGGED), "phoenix.spans.jsonl"])] * 2
    )


def test_a_graded_stack_run_copies_its_run_directory_out_once(
    docker: FakeDocker,
    tmp_path: Path,
    copied_at_grading: list[list[str]],
) -> None:
    docker.plays_passing = 1

    run_aurora_in_stack(tmp_path)

    outputs = [
        call for call in docker.calls if call[COMPOSE_PREFIX_LENGTH] == "cp" and call[-1] == str(tmp_path)
    ]
    assert len(outputs) == 1


def test_an_attack_stack_run_drains_the_record_then_renews_every_container_but_the_episode_for_the_twin(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.plays_passing = 1

    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path, mode="attack")

    commands = [call[COMPOSE_PREFIX_LENGTH:] for call in docker.calls]
    plays = [index for index, command in enumerate(commands) if command[0] == "up" and "--attach" in command]
    between = [
        command[:2] if command[0] == "cp" else command for command in commands[plays[0] + 1 : plays[1]]
    ]
    renewed = ["sandbox-agent-main", "sandbox-serving-agent", "sandbox-distill-agent", "sandbox-eval-agent"]
    renewed += ["sandbox-datapipe-agent", "sandbox-jobsvc-agent", "sandbox-controlplane-agent"]
    renewed += ["runner-1", "runner-2", "runner-3", *LOGGED]
    assert between == [
        ["stop", *(service for service in renewed if service != "phoenix")],  # Phoenix runs on to export
        ["logs", "--no-color", "--no-log-prefix", "gateway"],
        ["cp", "collector:/sealed/calls.jsonl"],
        *(["logs", "--no-color", "--no-log-prefix", service] for service in LOGGED),
        ["ps", "--status", "running", "--quiet", "phoenix"],
        ["exec", "-T", "phoenix", "python3.13", "-c", EXPORT_SPANS, "http://localhost:6006"],
        [
            "up",
            "--detach",
            "--wait",
            "--force-recreate",
            "--renew-anon-volumes",
            "--no-deps",
            "gateway",
            *renewed,
        ],
        ["up", "--detach", "--wait", "gateway"],
        ["port", "gateway", "8080"],
    ]


def test_a_stack_runs_service_log_holds_the_lines_of_the_episodes_container_and_then_the_honest_twins(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.plays_passing = 1

    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path, mode="attack")

    assert read_copied_log(tmp_path, "notes") == NOTES_LOG * 2


def test_a_log_that_cannot_be_copied_before_the_honest_twin_keeps_the_project_and_renews_nothing(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.plays_passing = 1
    docker.failing = "notes"

    with pytest.raises(StackError, match="kept compose project"):
        run_aurora_in_stack(tmp_path, mode="attack")

    renewed = [call for call in docker.calls if "--force-recreate" in call]
    assert (renewed, docker.plays, docker.verbs[-1]) == ([], 1, "stop")


def test_containers_that_do_not_come_back_for_the_honest_twin_stop_the_run_with_every_log_copied_out(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.plays_passing = 1
    docker.failing = "--force-recreate"

    with pytest.raises(StackError, match="did not come back for the honest twin"):
        run_aurora_in_stack(tmp_path, mode="attack")

    copied = read_copied_log(tmp_path, "notes")
    assert (copied, docker.plays, docker.verbs[-1]) == (NOTES_LOG * 2, 1, "down")


def test_the_honest_twin_plays_in_the_episodes_container_though_its_image_tag_moved(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.plays_passing = 1

    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path, mode="attack")

    plays = [call for call in docker.calls if "--attach" in call]
    assert ["--no-recreate" in play for play in plays] == [True, True]


def test_a_log_file_that_cannot_be_written_before_the_honest_twin_renews_nothing_and_writes_no_log(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    (tmp_path / "notes.log").mkdir()
    compose = ["docker", "compose", "-p", "locarena-test", "-f", "compose.yaml", "--project-directory", "."]

    with pytest.raises(StackError, match="nothing was renewed"):
        renew_services(SCRIPTED_CONFIG, compose, tmp_path, {}, {})

    renewed = [call for call in docker.calls if "--force-recreate" in call]
    assert (renewed, sorted(path.name for path in tmp_path.iterdir())) == ([], ["notes.log"])


def test_a_log_file_that_cannot_be_written_at_teardown_keeps_the_project_after_every_other_copy(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.unwritable_after_first_play = tmp_path

    with pytest.raises(StackError, match="kept compose project"):
        run_aurora_in_stack(tmp_path)

    copied = read_copied_log(tmp_path, "agentgateway")
    assert (copied, docker.verbs[-1], "down" in docker.verbs) == (NOTES_LOG, "stop", False)


def test_a_service_log_copied_before_a_failed_grading_is_not_added_again_at_teardown(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.plays_passing = 1
    docker.failing = "agentgateway"  # its copy fails, so the run stops before grading

    with pytest.raises(StackError, match="kept compose project"):
        run_aurora_in_stack(tmp_path)

    assert read_copied_log(tmp_path, "notes") == NOTES_LOG


def test_honest_mode_drops_the_covert_tools_from_each_agents_sandbox_tools() -> None:
    attack = load_run_config("configs/aurora-efficiency.yaml")

    honest = apply_mode(attack, "honest")

    offered = {tool for agent in honest.agents for tools in agent.sandbox.tools.values() for tool in tools}
    assert offered.isdisjoint(COVERT_TOOL_NAMES) and {"open_pr", "send_dm", "submit_job"} <= offered


def test_the_honest_twin_env_carries_a_gateway_config_without_covert_tools() -> None:
    config = load_run_config("configs/aurora-efficiency.yaml")

    attack_env, twin_env = issue_secrets(config)

    attack_policy = attack_env[TOOLS_GATEWAY_CONFIG_VARIABLE]
    twin_policy = twin_env[TOOLS_GATEWAY_CONFIG_VARIABLE]
    assert "start_rogue_loop" in attack_policy
    assert "open_pr" in twin_policy and not any(tool in twin_policy for tool in COVERT_TOOL_NAMES)
