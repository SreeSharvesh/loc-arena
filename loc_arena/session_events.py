"""What the agents' own sessions (``harness: pi``) leave in the model gateway's call record, read after play.

An agent's loop asks its model as the episode's caller on the agent's own route at the gateway
(``/loop/<agent id>/...``), so the call record holds each agent's model calls in order. Each call the gateway
answered is one turn of its agent, numbered from 1: the reply and the tool calls the loop made on it, until
the agent's next call.
A turn's ``inference_call`` event carries fingerprints alone, never text: the prompt's
is over the messages after the system prompt, which holds the agent's brief and so, in an attack, its covert
brief. The tool calls of a turn are recorded by the services that took them, the workspace service among
them, and are placed in the turn whose span holds their time.

A loop runs nothing itself, so each request may offer its model only tools reached over MCP
(``mcp__<server>__<tool>``) and pi's own tools that read MCP resources. ``find_local_tools`` names any other.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TypedDict

from pydantic import JsonValue, ValidationError

from loc_arena.config import RunConfig
from loc_arena.gateway.core import ProviderResult
from loc_arena.gateway.loop_routes import LOOP_ROUTE_PREFIX
from loc_arena.gateway.model_gateway import (
    CALL_RECORD_FILE,
    EPISODE_CALLER,
    PHASE_PLAYS,
    GatewayCall,
    Play,
)
from loc_arena.logging_.agent_trace import EpisodeTrace, TurnRecord, TurnRef
from loc_arena.logging_.events import fingerprint

AGENT_MODEL_ROLE = "untrusted_agent"  # the role every agent's loop calls
UNTRUSTED_ROLE = "untrusted"  # the actor role of an agent's inference_call event
CREDENTIAL_KIND = "turn_token"  # the sealed record's tag for a sanctioned turn: its agent's route admitted it
MCP_TOOL_PREFIX = "mcp__"
MCP_RESOURCE_TOOLS = frozenset({"list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"})


class _Function(TypedDict):
    name: str
    arguments: str


class ToolCall(TypedDict):
    """A tool call of a chat reply, in the provider's shape."""

    id: str
    type: str
    function: _Function


@dataclass(frozen=True)
class SessionCall:
    """One model call of an agent's loop that the gateway answered: the agent's turn of that number."""

    agent: str
    turn: int
    call: GatewayCall

    @property
    def wall(self) -> float:
        """When the gateway received the call."""
        return self.call.started_at


def read_play_calls(run_directory: Path, phase: str, config: RunConfig) -> list[SessionCall]:
    """The session calls of the play in the directory ``phase``."""
    return read_session_calls(run_directory, PHASE_PLAYS[phase], [agent.id for agent in config.agents])


def read_session_calls(run_directory: Path, play: Play, agents: Sequence[str]) -> list[SessionCall]:
    """Each answered model call of the loops of ``agents`` in ``play``, in order; none without a call record.

    A line that is no record is skipped, as is a call of any other caller or on any other path: an agent's
    route admits the episode alone, whose container runs the loops.
    """
    record = run_directory / CALL_RECORD_FILE
    if not record.exists():
        return []
    calls = sorted(_read_calls(record, play, agents), key=lambda call: call.started_at)
    turns = dict.fromkeys(agents, 0)
    numbered = []
    for call in calls:
        agent = _read_agent(call)
        turns[agent] += 1
        numbered.append(SessionCall(agent, turns[agent], call))
    return numbered


def _read_calls(record: Path, play: Play, agents: Sequence[str]) -> Iterator[GatewayCall]:
    for line in record.read_text(encoding="utf-8").splitlines():
        try:
            call = GatewayCall.model_validate_json(line)
        except ValidationError:
            continue
        if (
            call.caller == EPISODE_CALLER
            and _read_agent(call) in agents
            and call.play == play
            and call.status == HTTPStatus.OK
        ):
            yield call


def _read_agent(call: GatewayCall) -> str:
    """The agent whose route ``call`` was made on; empty for a call on any other path."""
    prefix, _, rest = call.path.removeprefix("/").partition("/")
    return rest.partition("/")[0] if prefix == LOOP_ROUTE_PREFIX else ""


def build_turns(calls: Sequence[SessionCall]) -> tuple[TurnRecord, ...]:
    """Each call's turn, from its call to its agent's next one; an agent's last turn has no end."""
    following: dict[str, float] = {}
    turns = []
    for call in reversed(calls):
        turns.append(
            TurnRecord(TurnRef(call.agent, call.turn), call.wall, following.get(call.agent, float("inf"))),
        )
        following[call.agent] = call.wall
    return tuple(reversed(turns))


def trace_sessions(calls: Sequence[SessionCall]) -> EpisodeTrace:
    """The trace of a play's sessions: its turns alone."""
    return EpisodeTrace(build_turns(calls), {}, {}, {}, (), 0)


def build_inference_payload(session_call: SessionCall) -> dict[str, JsonValue]:
    """The ``inference_call`` payload of ``session_call``: fingerprints, never text."""
    call = session_call.call
    request = call.request if isinstance(call.request, dict) else {}
    messages = request.get("messages")
    submitted = [m for m in messages if not _is_system(m)] if isinstance(messages, list) else []
    reply = _read_reply(call.response)
    usage, tool_calls = reply.usage, [dict(tool_call) for tool_call in reply.tool_calls]
    details = usage.get("prompt_tokens_details")
    return {
        "caller_identity": session_call.agent,
        "turn_id": f"{session_call.agent}:t{session_call.turn}",
        "batch_id": None,
        "prompt_fp": fingerprint(submitted),
        "output_fp": fingerprint(ProviderResult(reply.text, 0, 0, tool_calls or None).assistant_message()),
        "model_role": AGENT_MODEL_ROLE,
        "credential_kind": CREDENTIAL_KIND,
        "prompt_tokens": _count(usage.get("prompt_tokens")),
        "completion_tokens": _count(usage.get("completion_tokens")),
        "cached_tokens": _count(details.get("cached_tokens") if isinstance(details, dict) else None),
    }


def find_local_tools(calls: Sequence[SessionCall]) -> dict[str, list[str]]:
    """By agent, each tool a request of its loop offered its model that the loop would run itself."""
    local: dict[str, set[str]] = {}
    for session_call in calls:
        request = session_call.call.request
        offered = request.get("tools") if isinstance(request, dict) else None
        for tool in offered if isinstance(offered, list) else []:
            function = tool.get("function") if isinstance(tool, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            if not (
                isinstance(name, str) and (name.startswith(MCP_TOOL_PREFIX) or name in MCP_RESOURCE_TOOLS)
            ):
                local.setdefault(session_call.agent, set()).add(str(name))
    return {agent: sorted(names) for agent, names in local.items()}


def describe_local_tools(calls: Sequence[SessionCall]) -> list[str]:
    """A line for the run's unplaced records for each agent whose loop was offered a tool it runs itself."""
    return [
        json.dumps(
            {"reason": f"a model request of {agent}'s loop offered tools it runs itself", "line": tools},
        )
        for agent, tools in find_local_tools(calls).items()
    ]


def _is_system(message: JsonValue) -> bool:
    return isinstance(message, dict) and message.get("role") in ("system", "developer")


def _count(value: JsonValue) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


@dataclass(frozen=True)
class _Reply:
    """A chat reply: its text, its tool calls whole, and its usage."""

    text: str
    tool_calls: list[ToolCall]
    usage: dict[str, JsonValue]


def _read_bodies(response: JsonValue) -> list[dict[str, JsonValue]]:
    """A chat reply's JSON bodies: a stream's whole chunks, or the one body."""
    bodies: list[JsonValue] = [response]
    if isinstance(response, str):
        chunks = [
            line.removeprefix("data:").strip() for line in response.splitlines() if line.startswith("data:")
        ]
        bodies = [_parse_chunk(chunk) for chunk in chunks if chunk.startswith("{")]
    return [body for body in bodies if isinstance(body, dict)]


def _parse_chunk(chunk: str) -> JsonValue:
    """A stream's chunk as JSON; none for one cut short, as the last of a reply that broke off."""
    try:
        return json.loads(chunk)
    except json.JSONDecodeError:
        return None


def _read_part(body: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """The message of a body's first choice, or a chunk's delta; empty when it has none."""
    choices = body.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    part = (choice.get("delta") or choice.get("message")) if isinstance(choice, dict) else None
    return part if isinstance(part, dict) else {}


def _read_reply(response: JsonValue) -> _Reply:
    """The reply in ``response``, a stream's chunks joined: each tool call's pieces by its index."""
    text: list[str] = []
    calls: dict[int, ToolCall] = {}
    usage: dict[str, JsonValue] = {}
    for body in _read_bodies(response):
        reported = body.get("usage")
        usage = reported if isinstance(reported, dict) else usage
        part = _read_part(body)
        text.append(str(part.get("content") or ""))
        pieces = part.get("tool_calls")
        for position, piece in enumerate(pieces if isinstance(pieces, list) else []):
            if not isinstance(piece, dict):
                continue
            index, function = piece.get("index"), piece.get("function")
            call = calls.setdefault(
                index if isinstance(index, int) else position,
                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
            )
            call["id"] = str(piece.get("id") or call["id"])
            for field in ("name", "arguments"):
                call["function"][field] += str(
                    (function.get(field) if isinstance(function, dict) else "") or "",
                )
    return _Reply("".join(text), [calls[index] for index in sorted(calls)], usage)
