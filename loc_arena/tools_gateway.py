"""The tools gateway: agentgateway, putting every live service with tools on one MCP route.

Its config is generated per run from each agent's ``sandbox.tools`` and reaches it as a compose secret.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, TypedDict

from loc_arena.notices import NOTICE_TOOLS
from loc_arena.subagent_config import DELEGATE_TOOL, list_spawners, list_subagent_tools, name_lanes
from loc_arena.telemetry_stack import (
    TELEMETRY_NETWORK,
    GatewayTracing,
    find_reader,
    locate_reader,
    render_gateway_tracing,
)
from loc_arena.tool_authorization import (
    DECISION_FIELDS,
    list_enforced_services,
    render_authorization_policies,
)
from loc_arena.tool_authorization import (
    TOOLS_GATEWAY_SERVICE as TOOLS_GATEWAY_SERVICE,  # its compose service, by the name every module uses
)
from loc_arena.tool_list_notifier import TOOL_LIST_NOTIFIER
from loc_arena.workspace_service import WORKSPACE_SERVICE, WORKSPACE_TOOLS, subagent_token, workspace_key
from loc_arena.yaml_documents import dump_document

if TYPE_CHECKING:  # loc_arena.episode_stack imports this module
    from loc_arena.compose_document import ComposeService
    from loc_arena.config import RunConfig
    from loc_arena.episode_stack import Identity

TOOLS_GATEWAY_CONFIG_SECRET_NAME = "agentgateway_config"  # its config, generated per run: a compose secret
TOOLS_GATEWAY_CONFIG_VARIABLE = "LOC_ARENA_AGENTGATEWAY_CONFIG"  # where compose reads that config from
MCP_PATH = "/mcp"  # the MCP route of the tools gateway and of each service with tools


def serves_tools(config: RunConfig) -> bool:
    """Whether a stack run of ``config`` runs the tools gateway: a live service of its scenario has tools."""
    return any(service.tools for service in config.live_services)


def render_tools_gateway(config: RunConfig, tool_services: Sequence[str], network: str) -> ComposeService:
    """The tools gateway: its pinned image, on ``network`` alone, bounded as a live service, config a secret.

    It starts once every service it puts on its route is healthy.
    """
    stack = config.settings.stack
    reader = find_reader(config)  # the telemetry store's own MCP server, on the store's network alone
    started = {reader: {"condition": "service_healthy"}} if reader else {}
    return {
        "image": stack.tools_gateway_image,
        "command": ["-f", str(config.settings.gateway.secrets_dir / TOOLS_GATEWAY_CONFIG_SECRET_NAME)],
        "secrets": [TOOLS_GATEWAY_CONFIG_SECRET_NAME],
        "cap_drop": ["ALL"],
        "mem_limit": stack.service_memory_limit,
        "cpus": stack.service_cpus,
        "pids_limit": stack.service_pids_limit,
        "networks": [network, *([TELEMETRY_NETWORK] if reader else [])],
        "depends_on": {**{service: {"condition": "service_healthy"} for service in tool_services}, **started},
    }


def render_tools_gateway_config(
    config: RunConfig,
    tokens: Mapping[str, str],
    identities: Mapping[Identity, str],
    inbox_keys: Mapping[str, str] | None = None,
    credentials: Mapping[str, str] | None = None,
    notifier_key: str | None = None,
) -> str:
    """The tools gateway's config (agentgateway v1.6 schema) for agents with these sandbox ``tokens``.

    It admits each agent by the SHA-256 of its token, so it never holds one, and logs each request as a JSON
    line naming the agent, with a tool call's arguments and result or error. One CEL rule, generated from
    each agent's ``sandbox.tools``, allows an agent exactly those tools; a tool it may not call is left out of
    its tool list and refused before it reaches the service. Each service's target sets ``Authorization`` to
    the caller's identity on that service. Before a call of a tool of a service whose access the gateway
    enforces, it asks IAM with that service's credential, from ``credentials``, and goes on only on an allow
    (``loc_arena.tool_authorization``). With ``notifier_key`` the tool-list notifier, a target with no tools,
    is called with that key (``loc_arena.tool_list_notifier``). Tools are offered by their own names, never
    prefixed with their service's. It listens on IPv4 alone; admin, stats and readiness listeners are off, so
    nothing on agent-net can read this config back.
    """
    spawners = list_spawners(config)
    tools: dict[str, dict[str, Sequence[str]]] = {
        # An agent's session runs nothing itself: its bash, file and subscription tools are a target.
        agent.id: {
            **agent.sandbox.tools,
            WORKSPACE_SERVICE: [
                *WORKSPACE_TOOLS,
                *NOTICE_TOOLS,
                *([DELEGATE_TOOL] if agent.id in spawners else []),
            ],
        }
        for agent in config.agents
    }
    # A subagent holds a key of its own, keyed by the episode's, which names its agent, whose identities it is
    # given, and itself as the caller, whom the tools rule, the workspace service and the gateway's log go by.
    lanes = name_lanes(config) if notifier_key is not None else {}
    kinds = list(config.subagents.definitions)
    delegates = config.subagents.max_depth > 1
    for child, agent in lanes.items():
        tools[child] = _group_by_service(
            config,
            list_subagent_tools(config, agent, kinds, delegates=delegates),
        )
    callers = dict(tokens) | {child: subagent_token(str(notifier_key), child) for child in lanes}
    targets = _render_targets(config, tokens, identities, inbox_keys or {}, notifier_key, callers)
    policies = {
        "apiKey": {
            "mode": "strict",
            "location": {"header": {"name": "authorization", "prefix": "Bearer "}},
            "keys": [
                {
                    "keyHash": f"sha256:{hashlib.sha256(token.encode()).hexdigest()}",
                    "metadata": {"agent": lanes.get(caller, caller), "caller": caller},
                }
                for caller, token in callers.items()
            ],
        },
        # An agent with no tools on the target has no entry for it, so the rule fails to evaluate and denies.
        "mcpAuthorization": {
            "rules": [{"allow": f"mcp.tool.name in {json.dumps(tools)}[apiKey.caller][mcp.tool.target]"}],
        },
        **render_authorization_policies(config, identities, credentials or {}),
    }
    # A target that does not answer loses its own tools alone: the others' calls go on.
    backend = {"targets": targets, "prefixMode": "never", "failureMode": "failOpen"}
    document = {
        "config": {
            "enableIpv6": False,  # its default listener, [::], cannot bind where the kernel has no IPv6
            "adminAddr": "off",
            "statsAddr": "off",
            "readinessAddr": "off",
            "logging": {"format": "json"},
        },
        "frontendPolicies": _render_frontend_policies(config),
        "binds": [
            {
                "port": config.settings.stack.tools_gateway_port,
                "listeners": [
                    {"routes": [{"policies": policies, "backends": [{"mcp": backend}]}]},
                ],
            },
        ],
    }
    return dump_document(document)


def _group_by_service(config: RunConfig, offered: Sequence[str]) -> dict[str, Sequence[str]]:
    """``offered``, each tool under the service that serves it: the workspace's, or a live service's."""
    served = {service.name: service.tools for service in config.live_services} | {
        WORKSPACE_SERVICE: (*WORKSPACE_TOOLS, *NOTICE_TOOLS, DELEGATE_TOOL),
    }
    return {name: [tool for tool in tools if tool in offered] for name, tools in served.items()}


class Target(TypedDict, total=False):
    """One MCP target of the gateway's route: a service's tools, and what the gateway sets on a call to it."""

    name: str
    mcp: dict[str, str]
    policies: dict[str, dict[str, dict[str, dict[str, str]]]]


class FrontendPolicies(TypedDict, total=False):
    """agentgateway's ``frontendPolicies``: what it writes of each request."""

    accessLog: dict[str, dict[str, str]]  # the fields added to each line, each a CEL expression
    tracing: GatewayTracing


def _render_targets(
    config: RunConfig,
    tokens: Mapping[str, str],
    identities: Mapping[Identity, str],
    inbox_keys: Mapping[str, str],
    notifier_key: str | None,
    caller_tokens: Mapping[str, str],
) -> list[Target]:
    """Each service with tools as a target of the route, then the episode's: the workspace, and the notifier.

    A target is sent the calling agent's own credential on it: its identity on a service, its inbox key on
    IAM; the workspace is sent the caller's workspace key, from ``caller_tokens``, a subagent's its own.
    A ready image is sent none: it runs no IAM integration to know a caller by, and the gateway enforces
    IAM's decisions for it. The telemetry store's tools are its own MCP server's.
    """
    from loc_arena.stack_play import EPISODE_SERVICE  # noqa: PLC0415 - stack_play imports this module

    iam = config.settings.iam
    targets = []
    for service in config.live_services:
        if not service.tools:
            continue
        if iam.enabled and service.name == iam.service:
            # IAM's one tool reads the caller's inbox, so the gateway sends the agent's inbox key, which opens
            # nothing else, and never the agent's identity on IAM, which would let it manage IAM for it.
            callers: Mapping[str, str] | None = inbox_keys
        elif isinstance(service.source, str):
            callers = None
        else:
            callers = {
                key.agent_id: value for key, value in identities.items() if key.service == service.name
            }
        url = locate_reader(config, service) or f"http://{service.name}:{service.port}{MCP_PATH}"
        targets.append(_render_target(service.name, url, callers))
    workspace = f"http://{EPISODE_SERVICE}:{config.settings.stack.workspace_port}{MCP_PATH}"
    keys = {caller: workspace_key(token) for caller, token in caller_tokens.items()}
    targets.append(_render_target(WORKSPACE_SERVICE, workspace, keys, by="caller"))
    if notifier_key is not None:  # the same key for every agent's session, and no agent holds it
        notifier = workspace.removesuffix(MCP_PATH) + f"/{TOOL_LIST_NOTIFIER}{MCP_PATH}"
        targets.append(_render_target(TOOL_LIST_NOTIFIER, notifier, dict.fromkeys(tokens, notifier_key)))
    return targets


def _render_frontend_policies(config: RunConfig) -> FrontendPolicies:
    """What the gateway writes of each request: its log line, and a span where the company keeps telemetry.

    The line holds, beside the default MCP fields (method, target, tool name, session, source address), as
    its docs name them, the caller, each tool call's arguments and result or error, and the names of the tools
    each listing offered.
    """
    added = {
        "agent": "apiKey.agent",
        "caller": "apiKey.caller",  # the agent, or its subagent that made the call
        "tool_args": "mcp.tool.arguments",
        "tool_result": "mcp.tool.result",
        "tool_error": "mcp.tool.error",
        "tools_listed": "mcp.toolsList.tools.map(tool, tool.name)",  # what each tools/list offered
        **(DECISION_FIELDS if list_enforced_services(config) else {}),
    }
    policies: FrontendPolicies = {"accessLog": {"add": added}}
    if tracing := render_gateway_tracing(config):
        policies["tracing"] = tracing
    return policies


def _render_target(name: str, url: str, callers: Mapping[str, str] | None, by: str = "agent") -> Target:
    """The target ``name`` at ``url``, sent the credential ``callers`` holds for the key's ``by``, if any."""
    if callers is None:
        return {"name": name, "mcp": {"host": url}}
    authorization = f'"Bearer " + {json.dumps(dict(callers))}[apiKey.{by}]'
    return {
        "name": name,
        "mcp": {"host": url},
        "policies": {"transformations": {"request": {"set": {"authorization": authorization}}}},
    }
