"""The model gateway's routes for the agents' loops (``harness: pi``): one per agent, for its model calls.

An agent's loop calls ``/loop/<agent id>/<the chat path>``. Its route admits the episode's token alone, whose
container runs the loops, and as many calls in a play as the agent's turn budget: a call past it is refused
(429). The call record names the route's path, and so the agent. Each call goes upstream with the request
fields the run config sets for the agents' model, whatever the loop sent for them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import JsonValue

LOOP_ROUTE_PREFIX = "loop"
AGENTS_KEY_SECRET_NAME = "agents_upstream_key"  # the key of gateway.agents_upstream, when a run sets one
NEVER_REFILLS = "87600h"  # a rate-limit bucket that refills once in ten years counts calls per play


@dataclass(frozen=True)
class Loops:
    """The agents' loops that call the gateway themselves.

    ``calls`` holds how many calls each agent's loop may make in a play; every one goes upstream with
    ``overrides``, the request fields the run config sets for the agents' model: sampling, routing, reasoning.
    """

    calls: Mapping[str, int]
    overrides: Mapping[str, JsonValue]


def render_loop_routes(
    loops: Loops | None,
    policies: Mapping[str, Any],
    backends: Sequence[Mapping[str, Any]],
    chat_path: str,
    upstream_chat_path: str,
) -> list[dict[str, Any]]:
    """Each loop's route (agentgateway v1.6 schema): ``policies``, the episode alone admitted, and its own.

    The episode calls a loop on ``chat_path``; its calls reach ``backends`` on ``upstream_chat_path``.
    """
    if loops is None:
        return []
    return [
        {
            "name": f"{LOOP_ROUTE_PREFIX}-{agent}",
            "matches": [{"path": {"exact": f"/{LOOP_ROUTE_PREFIX}/{agent}/{chat_path}"}}],
            "policies": {
                **policies,
                # Parsed as a chat completion, so the overrides are set on it.
                "ai": {"routes": {"*": "completions"}, "overrides": dict(loops.overrides)},
                "authorization": {"rules": [{"allow": "size(request.body) >= 0"}]},
                "urlRewrite": {"path": {"full": f"/{upstream_chat_path}"}},
                "localRateLimit": [
                    {
                        "type": "requests",
                        "maxTokens": calls,
                        "tokensPerFill": 1,
                        "fillInterval": NEVER_REFILLS,
                    },
                ],
            },
            "backends": list(backends),
        }
        for agent, calls in loops.calls.items()
    ]
