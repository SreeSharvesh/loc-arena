"""The tools gateway: agentgateway, putting every live service with tools on one MCP route.

Its config is generated per run from each agent's ``sandbox.tools`` and reaches it as a compose secret.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from loc_arena.workspace_service import WORKSPACE_SERVICE, WORKSPACE_TOOLS, workspace_key
from loc_arena.yaml_documents import dump_document

if TYPE_CHECKING:  # loc_arena.episode_stack imports this module
    from loc_arena.compose_document import ComposeService
    from loc_arena.config import RunConfig
    from loc_arena.episode_stack import Identity

TOOLS_GATEWAY_SERVICE = "agentgateway"  # its compose service and host name on agent-net
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
    return {
        "image": stack.tools_gateway_image,
        "command": ["-f", str(config.settings.gateway.secrets_dir / TOOLS_GATEWAY_CONFIG_SECRET_NAME)],
        "secrets": [TOOLS_GATEWAY_CONFIG_SECRET_NAME],
        "cap_drop": ["ALL"],
        "mem_limit": stack.service_memory_limit,
        "cpus": stack.service_cpus,
        "pids_limit": stack.service_pids_limit,
        "networks": [network],
        "depends_on": {service: {"condition": "service_healthy"} for service in tool_services},
    }


def render_tools_gateway_config(
    config: RunConfig,
    tokens: Mapping[str, str],
    identities: Mapping[Identity, str],
    inbox_keys: Mapping[str, str] | None = None,
) -> str:
    """The tools gateway's config (agentgateway v1.6 schema) for agents with these sandbox ``tokens``.

    It admits each agent by the SHA-256 of its token, so it never holds one, and logs each request as a JSON
    line naming the agent, with a tool call's arguments and result or error. One CEL rule, generated from
    each agent's ``sandbox.tools``, allows an agent exactly those tools; a tool it may not call is left out of
    its tool list and refused before it reaches the service. Each service's target sets ``Authorization`` to
    the caller's identity on that service. Tools are offered by their own names, never prefixed with their
    service's. It listens on IPv4 alone; admin, stats and readiness listeners are off, so nothing on agent-net
    can read this config back.
    """
    tools = {agent.id: dict(agent.sandbox.tools) for agent in config.agents}
    workspace = []
    if config.harness == "pi":  # each agent's loop runs nothing itself: its bash and file tools are a target
        from loc_arena.stack_play import EPISODE_SERVICE  # noqa: PLC0415 - stack_play imports this module

        port = config.settings.stack.workspace_port
        keys = {agent: workspace_key(token) for agent, token in tokens.items()}
        workspace = [_render_target(WORKSPACE_SERVICE, f"http://{EPISODE_SERVICE}:{port}{MCP_PATH}", keys)]
        tools = {
            agent: {**offered, WORKSPACE_SERVICE: list(WORKSPACE_TOOLS)} for agent, offered in tools.items()
        }
    iam = config.settings.iam
    callers = {
        # IAM's one tool reads the caller's inbox, so the gateway sends the agent's inbox key, which opens
        # nothing else, and never the agent's identity on IAM, which would let the gateway manage IAM for it.
        service: dict(inbox_keys or {})
        if iam.enabled and service.name == iam.service
        else {
            identity.agent_id: value
            for identity, value in identities.items()
            if identity.service == service.name
        }
        for service in config.live_services
        if service.tools
    }
    targets = [
        *(
            _render_target(service.name, f"http://{service.name}:{service.port}{MCP_PATH}", caller)
            for service, caller in callers.items()
        ),
        *workspace,
    ]
    policies = {
        "apiKey": {
            "mode": "strict",
            "location": {"header": {"name": "authorization", "prefix": "Bearer "}},
            "keys": [
                {
                    "keyHash": f"sha256:{hashlib.sha256(tokens[agent.id].encode()).hexdigest()}",
                    "metadata": {"agent": agent.id},
                }
                for agent in config.agents
            ],
        },
        # An agent with no tools on the target has no entry for it, so the rule fails to evaluate and denies.
        "mcpAuthorization": {
            "rules": [{"allow": f"mcp.tool.name in {json.dumps(tools)}[apiKey.agent][mcp.tool.target]"}],
        },
    }
    backend = {"targets": targets, "prefixMode": "never"}
    document = {
        "config": {
            "enableIpv6": False,  # its default listener, [::], cannot bind where the kernel has no IPv6
            "adminAddr": "off",
            "statsAddr": "off",
            "readinessAddr": "off",
            "logging": {"format": "json"},
        },
        # Beside the default MCP fields (method, target, tool name, session, source address), as its docs name
        # them: the caller, and each tool call's arguments and result or error.
        "frontendPolicies": {
            "accessLog": {
                "add": {
                    "agent": "apiKey.agent",
                    "tool_args": "mcp.tool.arguments",
                    "tool_result": "mcp.tool.result",
                    "tool_error": "mcp.tool.error",
                },
            },
        },
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


def _render_target(name: str, url: str, callers: Mapping[str, str]) -> dict[str, Any]:
    """The target ``name`` at ``url``, sent the calling agent's own bearer credential from ``callers``."""
    authorization = f'"Bearer " + {json.dumps(dict(callers))}[apiKey.agent]'
    return {
        "name": name,
        "mcp": {"host": url},
        "policies": {"transformations": {"request": {"set": {"authorization": authorization}}}},
    }
