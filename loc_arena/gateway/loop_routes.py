"""The model gateway's routes for the agents' loops (``harness: pi``): one per agent, for its model calls.

An agent's loop calls ``/loop/<agent id>/<the chat path>``, and each subagent it starts
``/loop/<subagent id>/<the chat path>`` on the same route. The route admits the episode's token alone, whose
container runs the loops, and as many calls in a play as the agent's turn budget, its subagents' among them:
a call past it is refused (429). The call record names the path, and so the agent or the subagent. Each
call goes upstream with the request fields the run config sets for that agent's model, whatever the loop
sent for them.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import JsonValue, SecretStr

LOOP_ROUTE_PREFIX = "loop"
AGENTS_KEY_SECRET_NAME = "agents_upstream_key"  # the key of gateway.agents_upstream, when a run sets one
NEVER_REFILLS = "87600h"  # a rate-limit bucket that refills once in ten years counts calls per play
_REQUESTED_MODEL = "json(request.body).model"  # the model a call names, read before the gateway parses it


@dataclass(frozen=True)
class Loops:
    """The agents' loops that call the gateway themselves.

    ``calls`` holds how many calls each agent's loop may make in a play; every one goes upstream with that
    agent's ``overrides``, the request fields the run config sets for its model: sampling, routing, reasoning.
    """

    calls: Mapping[str, int]
    overrides: Mapping[str, Mapping[str, JsonValue]]  # by agent, as calls is
    # The one caller the loops' routes admit, and its token; none: the episode.
    caller: tuple[str, SecretStr] | None = None
    allow: str = "size(request.body) >= 0"  # the rule a call passes: any body, read so the record keeps it
    children: Mapping[str, Sequence[str]] = field(default_factory=dict)  # by agent: its subagents' paths
    # By agent whose subagents may choose a model: each model it admits, with that model's request fields.
    models: Mapping[str, Mapping[str, Mapping[str, JsonValue]]] = field(default_factory=dict)


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
            "matches": [
                {"path": {"exact": f"/{LOOP_ROUTE_PREFIX}/{lane}/{chat_path}"}}
                for lane in (agent, *loops.children.get(agent, ()))
            ],
            "policies": {
                **policies,
                **_render_request_fields(loops, agent),
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


def _render_request_fields(loops: Loops, agent: str) -> dict[str, Any]:
    """How ``agent``'s route sets each call's request fields, and which calls it admits.

    Parsed as a chat completion, a call gets its agent's overrides. A route whose subagents may choose a model
    sets each field from the model the call names, drops one that model has none of, and admits only those
    models.
    """
    if agent not in loops.models:
        ai = {"routes": {"*": "completions"}, "overrides": dict(loops.overrides[agent])}
        return {"ai": ai, "authorization": {"rules": [{"allow": loops.allow}]}}
    models, named = loops.models[agent], _REQUESTED_MODEL
    fields = dict.fromkeys(field for settings in models.values() for field in settings)
    transformations = {
        field: f"{json.dumps({m: s[field] for m, s in models.items() if field in s})}[{named}]"
        for field in fields
    }
    allow = f"{named} in {json.dumps(list(models))} && !has(json(request.body).models)"  # no fallback list
    return {
        "ai": {"routes": {"*": "completions"}, "transformations": transformations},
        "authorization": {"rules": [{"allow": allow}]},
    }
