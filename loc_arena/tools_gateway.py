"""The tools gateway: agentgateway, putting every live service with tools on one MCP route.

Its config is generated per run from each agent's ``sandbox.tools`` and reaches it as a compose secret.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:  # loc_arena.episode_stack imports this module
    from loc_arena.config import RunConfig
    from loc_arena.episode_stack import Identity

TOOLS_GATEWAY_SERVICE = "agentgateway"  # its compose service and host name on agent-net
TOOLS_GATEWAY_CONFIG_SECRET_NAME = "agentgateway_config"  # its config, generated per run: a compose secret
TOOLS_GATEWAY_CONFIG_VARIABLE = "LOC_ARENA_AGENTGATEWAY_CONFIG"  # where compose reads that config from
MCP_PATH = "/mcp"  # the MCP route of the tools gateway and of each service with tools


def serves_tools(config: RunConfig) -> bool:
    """Whether a stack run of ``config`` runs the tools gateway: a live service of its scenario has tools."""
    return any(service.tools for service in config.live_services)


def render_tools_gateway_config(
    config: RunConfig,
    tokens: Mapping[str, str],
    identities: Mapping[Identity, str],
) -> str:
    """The tools gateway's config (agentgateway v1.5 schema) for agents with these sandbox ``tokens``.

    It admits each agent by the SHA-256 of its token, so it never holds one, and logs each request as a JSON
    line naming the agent, with a tool call's arguments and result or error. One CEL rule, generated from
    each agent's ``sandbox.tools``, allows an agent exactly those tools; a tool it may not call is left out of
    its tool list and refused before it reaches the service. Each service's target sets ``Authorization`` to
    the caller's identity on that service. Admin, stats and readiness listeners are off, so nothing on
    agent-net can read this config back.
    """
    tools = {agent.id: dict(agent.sandbox.tools) for agent in config.agents}
    callers = {
        service: {
            identity.agent_id: value
            for identity, value in identities.items()
            if identity.service == service.name
        }
        for service in config.live_services
        if service.tools
    }
    targets = [
        {
            "name": service.name,
            "mcp": {"host": f"http://{service.name}:{service.port}{MCP_PATH}"},
            "policies": {
                "transformations": {
                    "request": {"set": {"authorization": f'"Bearer " + {json.dumps(caller)}[apiKey.agent]'}},
                },
            },
        }
        for service, caller in callers.items()
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
    document = {
        "config": {
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
                    {
                        "routes": [
                            {
                                "policies": policies,
                                # Names stay unprefixed and route by lookup: they are unique across services,
                                # so an agent calls open_pr, not forge_open_pr, in a stack as in process.
                                "backends": [{"mcp": {"targets": targets, "prefixMode": "never"}}],
                            },
                        ],
                    },
                ],
            },
        ],
    }
    return yaml.safe_dump(document, sort_keys=False)
