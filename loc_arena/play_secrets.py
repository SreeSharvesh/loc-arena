"""Each play's secrets, which reach compose through its environment alone, and the identities they hold."""

from __future__ import annotations

import secrets

from loc_arena import deploy_pool, telemetry_stack
from loc_arena.config import RunConfig, sandbox_service
from loc_arena.gateway import model_gateway
from loc_arena.iam_bootstrap import (
    BOOTSTRAP_VARIABLE,
    inbox_key_variable,
    managed_services,
    render_bootstrap,
    service_credential_variable,
)
from loc_arena.identity_variables import Identity, find_shared_identity_variable, holds_identities
from loc_arena.node_pool import list_node_services, node_token_variable
from loc_arena.own_sessions import describe_loops
from loc_arena.runner_pool import list_runner_services, runner_token_variable
from loc_arena.sandbox import token_secret_name
from loc_arena.stack_play import StackError
from loc_arena.subagent_config import name_lanes
from loc_arena.tool_authorization import list_enforced_services
from loc_arena.tool_list_notifier import NOTIFIER_KEY_VARIABLE
from loc_arena.tools_gateway import TOOLS_GATEWAY_CONFIG_VARIABLE, render_tools_gateway_config, serves_tools

TOKEN_BYTES = 32  # the entropy of each sandbox token and of each agent's identity on a live service


def issue_identities(config: RunConfig) -> tuple[Identity, ...]:
    """Each agent's identity on each live service with rights or tools, each subagent's on each with tools.

    A subagent holds none on IAM, and only the tools gateway holds its. Refuses two sharing a host variable.
    """
    agents, lanes = [agent.id for agent in config.agents], list(name_lanes(config))
    if shared := find_shared_identity_variable([*agents, *lanes], config.live_services):
        raise StackError(shared)
    by_tools = [*agents, *lanes]
    return tuple(
        Identity(service.name, holder)
        for service in config.live_services
        if holds_identities(service)
        for holder in (by_tools if service.tools and service.name != config.settings.iam.service else agents)
    )


def token_variable(agent_id: str) -> str:
    """Where compose reads the token of ``agent_id``'s sandbox from, on the host."""
    return f"LOC_ARENA_{token_secret_name(agent_id).upper()}"


def issue_secrets(config: RunConfig) -> tuple[dict[str, str], dict[str, str]]:
    """Every secret of a stack run, by the host variable compose reads: the attack env and the twin's."""
    tokens = {agent.id: secrets.token_urlsafe(TOKEN_BYTES) for agent in config.agents}
    held = model_gateway.issue_held_tokens(TOKEN_BYTES)
    sandboxes = {sandbox_service(agent): token for agent, token in tokens.items()}
    badges = bool(managed_services(config))  # IAM runs and signs workloads' badges

    def with_gateway(environment: dict[str, str], play: model_gateway.Play) -> dict[str, str]:
        gateway_secrets = model_gateway.issue_gateway_secrets(
            config.settings,
            held,
            sandboxes,
            play,
            badges=badges,
            loops=describe_loops(config),
        )
        return environment | gateway_secrets | telemetry_stack.issue_telemetry_secrets(config, TOKEN_BYTES)

    attack, twin = _issue_play_secrets(config, tokens)
    return with_gateway(attack, "attack"), with_gateway(twin, "twin")


def _issue_play_secrets(config: RunConfig, tokens: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Each play's secrets but the model gateway's, with these sandbox ``tokens``: attack env and twin's.

    They reach compose through its environment alone: never a command line or a file. The honest twin's env
    differs in every identity, IAM credential and inbox key: the sandboxes and services are recreated with new
    ones, so nothing the attack play held authenticates in the twin.
    """
    managed = managed_services(config)
    runners = list_runner_services(config)
    nodes = list_node_services(config)
    notifier_key = secrets.token_urlsafe(TOKEN_BYTES)  # once, as the episode that holds it is not recreated

    def issue_for_play() -> dict[str, str]:
        identities = {identity: secrets.token_urlsafe(TOKEN_BYTES) for identity in issue_identities(config)}
        environment = {
            **{token_variable(agent): token for agent, token in tokens.items()},
            **{identity.variable: value for identity, value in identities.items()},
            **{runner_token_variable(runner): secrets.token_urlsafe(TOKEN_BYTES) for runner in runners},
            **{node_token_variable(node): secrets.token_urlsafe(TOKEN_BYTES) for node in nodes},
            **deploy_pool.issue_release_tokens(config, TOKEN_BYTES),
        }
        if managed:
            environment[BOOTSTRAP_VARIABLE] = render_bootstrap(config)
            for name in managed:
                environment[service_credential_variable(name)] = secrets.token_urlsafe(TOKEN_BYTES)
            for agent in config.agents:  # per play, so a key from the attack play reads nothing in the twin
                environment[inbox_key_variable(agent.id)] = secrets.token_urlsafe(TOKEN_BYTES)
        if serves_tools(config):
            environment[NOTIFIER_KEY_VARIABLE] = notifier_key
            inbox_keys = (
                {agent.id: environment[inbox_key_variable(agent.id)] for agent in config.agents}
                if managed
                else None
            )
            environment[TOOLS_GATEWAY_CONFIG_VARIABLE] = render_tools_gateway_config(
                config,
                tokens,
                identities,
                inbox_keys,
                # The gateway asks IAM as each service it enforces for, with that service's credential.
                {
                    name: environment[service_credential_variable(name)]
                    for name in list_enforced_services(config)
                },
                environment[NOTIFIER_KEY_VARIABLE],
            )
        return environment

    return issue_for_play(), issue_for_play()
