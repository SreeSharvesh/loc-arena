"""The rendered compose file keeps the key and the route out in the gateway, the episode on agent-net."""

from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.episode_stack import AGENT_NETWORK, EGRESS_NETWORK, ComposeDocument, render_compose
from loc_arena.gateway import core
from loc_arena.settings import LocArenaSettings, StackSettings

REPOSITORY = Path("/repository")


def render(settings: LocArenaSettings | None = None) -> ComposeDocument:
    return render_compose(settings or LocArenaSettings(), REPOSITORY, "a-run", ["--mode", "honest"])


def test_only_the_gateway_is_on_the_network_with_a_route_out() -> None:
    compose = render()

    on_egress = [
        name for name, service in compose["services"].items() if EGRESS_NETWORK in service["networks"]
    ]

    assert (on_egress, compose["networks"][EGRESS_NETWORK]["internal"]) == (["gateway"], False)


def test_the_episode_is_only_on_the_network_with_no_route_out() -> None:
    compose = render()

    episode_networks = compose["services"]["episode"]["networks"]

    assert (episode_networks, compose["networks"][AGENT_NETWORK]["internal"]) == ([AGENT_NETWORK], True)


def test_only_the_gateway_receives_the_key() -> None:
    compose = render()

    holders = [name for name, service in compose["services"].items() if service.get("secrets")]

    assert (holders, compose["secrets"]) == (
        ["gateway"],
        {"openrouter_api_key": {"environment": "OPENROUTER_API_KEY"}},
    )


def test_the_episode_sends_its_model_calls_to_the_gateway() -> None:
    compose = render()

    environment = compose["services"]["episode"]["environment"]

    assert environment == {core.GATEWAY_URL_VARIABLE: "http://gateway:8080/api/v1/chat/completions"}


def test_the_episode_runs_the_requested_run_and_mode() -> None:
    compose = render()

    command = compose["services"]["episode"]["command"]

    assert command[3:] == ["run", "--run", "a-run", "--mode", "honest", "--out", "/output"]


def test_the_episode_limits_come_from_the_stack_settings() -> None:
    settings = LocArenaSettings(stack=StackSettings(episode_cpus=0.5, episode_memory_limit="1g"))

    episode = render(settings)["services"]["episode"]

    assert (episode["cpus"], episode["mem_limit"]) == (0.5, "1g")


def test_subprocesses_running_agent_code_do_not_inherit_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(core.API_KEY_VARIABLE, "sk-real")

    environment = core.environment_without_key()

    assert core.API_KEY_VARIABLE not in environment
