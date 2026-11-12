"""The model gateway's routes for the agents' loops (``harness: pi``): one per agent, for its model calls.

An agent's loop calls ``/loop/<agent id>/<the chat path>``. Its route admits the episode's token alone, whose
container runs the loops, and as many calls in a play as the agent's turn budget: a call past it is refused
(429). The call record names the route's path, and so the agent. Each call goes upstream with the request
fields the run config sets for the agents' model, whatever the loop sent for them.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import JsonValue, SecretStr

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
    # The one caller the loops' routes admit, and its token; none: the episode.
    caller: tuple[str, SecretStr] | None = None
    allow: str = "size(request.body) >= 0"  # the rule a call passes: any body, read so the record keeps it


def render_loop_routes(
    loops: Loops | None,
    policies: Mapping[str, Any],
    backends: Sequence[Mapping[str, Any]],
    chat_path: str,
    upstream_chat_path: str,
) -> list[dict[str, Any]]:
    """Each loop's route (agentgateway v1.6 schema): ``policies``, its one caller alone admitted, and its own.

    The episode calls a loop on ``chat_path``; its calls reach ``backends`` on ``upstream_chat_path``.
    """
    if loops is None:
        return []
    if loops.caller is not None:
        name, token = loops.caller
        key = {
            "keyHash": f"sha256:{hashlib.sha256(token.get_secret_value().encode()).hexdigest()}",
            "metadata": {"caller": name},
        }
        policies = {**policies, "apiKey": {**policies["apiKey"], "keys": [key]}}
    return [
        {
            "name": f"{LOOP_ROUTE_PREFIX}-{agent}",
            "matches": [{"path": {"exact": f"/{LOOP_ROUTE_PREFIX}/{agent}/{chat_path}"}}],
            "policies": {
                **policies,
                # Parsed as a chat completion, so the overrides are set on it.
                "ai": {"routes": {"*": "completions"}, "overrides": dict(loops.overrides)},
                "authorization": {"rules": [{"allow": loops.allow}]},
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
