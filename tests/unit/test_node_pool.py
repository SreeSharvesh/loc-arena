"""A stack run's fleet: ``node-1`` to ``node-<n>``, worker containers only the job service drives.

Read from the compose file a stack run renders, for the aurora run, whose job service has three nodes.
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest
import yaml
from loc_arena.compose_document import ComposeDocument, ServiceSecret
from loc_arena.config import load_run_config
from loc_arena.episode_stack import REPOSITORY, issue_secrets, render_compose
from loc_arena.gateway.model_gateway import MODEL_GATEWAY_CONFIG_VARIABLE
from loc_arena.node_pool import node_token_variable
from sandbox_server.node import NodeServerSettings

RUN = "aurora-efficiency"
AURORA = load_run_config(REPOSITORY / "configs" / f"{RUN}.yaml")
OWNER = "jobsvc"
NODES = ("node-1", "node-2", "node-3")


def secret_sources(secrets: list[str | ServiceSecret]) -> set[str]:
    return {secret if isinstance(secret, str) else secret["source"] for secret in secrets}


@pytest.fixture
def compose() -> ComposeDocument:
    return render_compose(AURORA, REPOSITORY, RUN, [])


def test_each_node_runs_the_worker_image_on_agent_net_alone_with_no_capability_or_port(
    compose: ComposeDocument,
) -> None:
    nodes = [compose["services"][node] for node in NODES]

    shape = {(node["image"], tuple(node["networks"]), tuple(node["cap_drop"])) for node in nodes}
    assert (shape, [node.get("ports") for node in nodes]) == (
        {("loc-arena-worker:latest", ("agent-net",), ("ALL",))},
        [None, None, None],
    )


def test_only_the_first_node_builds_the_image_the_others_run(compose: ComposeDocument) -> None:
    built = [
        ("build" in compose["services"][node], compose["services"][node].get("pull_policy")) for node in NODES
    ]

    assert built == [(True, None), (False, "never"), (False, "never")]


def test_a_nodes_daemon_serves_the_job_service_alone_in_an_empty_workspace_under_its_own_name(
    compose: ComposeDocument,
) -> None:
    settings = NodeServerSettings.model_validate_json(compose["services"]["node-2"]["command"][-1])

    assert (settings.trusted_caller, settings.node_id, str(settings.workspace_directory)) == (
        OWNER,
        "node-2",
        "/workspace",
    )


def test_a_node_mounts_the_checkouts_and_the_skills_read_only_and_gives_a_job_its_cpus_and_memory(
    compose: ComposeDocument,
) -> None:
    node = compose["services"]["node-1"]
    settings = NodeServerSettings.model_validate_json(node["command"][-1])

    skills = REPOSITORY / "scenarios" / "aurora_efficiency" / "skills"
    mounts = ["checkouts:/checkouts:ro", f"{skills}:/skills:ro"]
    assert (node["volumes"], node["cpus"], node["mem_limit"]) == (mounts, 2.0, "2048m")
    assert str(settings.checkouts_directory) == "/checkouts"


def test_a_nodes_token_is_mounted_in_that_node_and_the_job_service_alone(compose: ComposeDocument) -> None:
    holders = {
        name
        for name, service in compose["services"].items()
        if "node_token_node_1" in secret_sources(service.get("secrets", []))
    }

    assert holders == {"node-1", OWNER}


def test_a_nodes_jobs_call_the_gateway_as_that_node_on_the_runners_route_for_a_model_it_allows(
    compose: ComposeDocument,
) -> None:
    attack, _ = issue_secrets(AURORA)
    settings = NodeServerSettings.model_validate_json(compose["services"]["node-1"]["command"][-1])

    routes = yaml.safe_load(attack[MODEL_GATEWAY_CONFIG_VARIABLE])["binds"][0]["listeners"][0]["routes"]
    runners = next(route for route in routes if route["name"] == "runners")["policies"]["apiKey"]["keys"]
    digest = hashlib.sha256(attack[node_token_variable("node-1")].encode()).hexdigest()
    assert (settings.gateway_url, settings.gateway_model) == (
        "http://gateway:8080/runner/api/v1",
        "deepseek/deepseek-v4.1-flash",
    )
    assert {"keyHash": f"sha256:{digest}", "metadata": {"caller": "node-1", "role": "runner"}} in runners


def test_each_play_gives_every_node_a_new_token() -> None:
    attack, twin = issue_secrets(AURORA)

    assert all(attack[node_token_variable(node)] != twin[node_token_variable(node)] for node in NODES)


def test_a_run_whose_scenario_has_no_fleet_renders_no_node() -> None:
    services = tuple(dataclasses.replace(service, nodes=()) for service in AURORA.live_services)

    rendered = render_compose(dataclasses.replace(AURORA, live_services=services), REPOSITORY, RUN, [])

    assert [name for name in rendered["services"] if name.startswith("node-")] == []
