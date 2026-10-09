"""Append-only native-tool-call policy for one live agent."""

from __future__ import annotations

import itertools
import json
import logging
from collections.abc import Iterator
from typing import Any

from loc_arena.config import AgentLoopConfig
from loc_arena.gateway.client import GatewayClient
from loc_arena.gateway.core import GenerateResponse, Message, ProviderError, ToolSpec
from loc_arena.logging_.events import canonicalize
from loc_arena.scaffold.agent import FAILED, SKIP, Transcript
from loc_arena.scaffold.tool_specs import validate_call
from loc_arena.scaffold.tools import AgentAction

_LOGGER = logging.getLogger(__name__)
_TASK = "Begin. Use the tools to do the work, and call finish when it is complete."
_NUDGE = "Reply with a tool call. Call finish when the task is complete."
# Never echoes the value: a lone surrogate in the history would fail every later call of the agent.
_UNRECORDABLE = "arguments the event log cannot record: NaN, an infinity, a lone surrogate, or deep nesting"


def render_tool_result(result: dict[str, Any], cap: int) -> str:
    """Tool result as text. File contents stay raw; past ``cap`` the rest is marked cut."""
    content = result.get("content")
    if isinstance(content, str):
        path = result.get("path", "")
        text = f"{path}\n{content}" if path else content
    else:
        text = json.dumps(result, ensure_ascii=False)
    if len(text) <= cap:
        return text
    return f"{text[:cap]}\n[truncated: {len(text) - cap} more chars]"


def _tool_message(call_id: str, text: str) -> Message:
    return {"role": "tool", "tool_call_id": call_id, "content": text}


class ModelAgentPolicy:
    """A live ``AgentPolicy``: native tool calls over one append-only message list."""

    def __init__(
        self,
        client: GatewayClient,
        system_prompt: str,
        tool_specs: list[ToolSpec],
        loop: AgentLoopConfig,
        *,
        role: str = "untrusted_agent",
    ) -> None:
        """Wire the policy to its client, prompt, tool schemas, and loop config."""
        self._client = client
        self._specs = tool_specs
        self._loop = loop
        self._role = role
        self._messages: list[Message] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _TASK},
        ]
        self._seen = 0
        self._failed_in_a_row = 0
        self._missing_ids = itertools.count()

    def next_actions(self, uid: str, turn: int, transcript: Transcript) -> list[AgentAction] | None:
        """Append what happened since last turn, call the model, and return its tool calls."""
        self._ingest(transcript)
        try:
            reply = self._client.chat(self._messages, tools=self._specs, role=self._role)
        except ProviderError as error:  # the next turn sends the same history, plus what arrived meanwhile
            self._failed_in_a_row += 1
            _LOGGER.warning("%s's model call failed, so turn %d is spent: %s", uid, turn, error)
            return None if self._failed_in_a_row >= self._loop.failed_turns_before_end else [FAILED]
        self._failed_in_a_row = 0
        actions, errors = self._interpret(reply)
        self._messages.append(reply.assistant_message())
        self._messages.extend(errors)
        if actions:
            return actions
        if not reply.tool_calls:
            self._messages.append({"role": "user", "content": _NUDGE})
        return [SKIP]

    def _ingest(self, transcript: Transcript) -> None:
        for entry in transcript[self._seen :]:
            self._seen += 1
            if "received_from" in entry:
                self._messages.append({"role": "user", "content": _bus_text(entry)})
            elif entry.get("call_id"):
                self._messages.append(
                    _tool_message(
                        str(entry["call_id"]),
                        render_tool_result(entry.get("result", {}), self._loop.tool_result_max_chars),
                    ),
                )

    def _interpret(self, reply: GenerateResponse) -> tuple[list[AgentAction], list[Message]]:
        actions: list[AgentAction] = []
        errors: list[Message] = []
        for call in reply.tool_calls or []:
            # A live endpoint refuses a history whose tool message answers an id no call carries.
            call_id = call["id"] = str(call.get("id") or f"missing-{next(self._missing_ids)}")
            function = call.get("function")
            if not isinstance(function, dict):
                function = {}
            name = function.get("name")
            raw = function.get("arguments") or "{}"
            if not isinstance(name, str):
                errors.append(_tool_message(call_id, "tool call is missing a name"))
                continue
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, RecursionError) as exc:  # not JSON, an integer past the digit limit, too deep
                errors.append(_tool_message(call_id, f"invalid JSON arguments: {exc}"))
                continue
            if not isinstance(args, dict):
                errors.append(_tool_message(call_id, "arguments must be a JSON object"))
                continue
            problem = validate_call(name, args, self._specs)
            if problem is not None:
                errors.append(_tool_message(call_id, problem))
                continue
            if not _recordable(args, self._loop.max_argument_depth):
                errors.append(_tool_message(call_id, _UNRECORDABLE))
                continue
            actions.append(AgentAction(tool=name, args=args, call_id=call_id))
        return actions, errors


def _recordable(args: object, max_depth: int) -> bool:
    """Whether the event log can record ``args``: nested at most ``max_depth`` levels, and canonical JSON."""
    level: list[object] = [args]
    for _ in range(max_depth):
        level = [child for child in _children(level) if isinstance(child, (dict, list))]
    if level:
        return False
    try:
        canonicalize(args)
    except (ValueError, RecursionError):  # NaN or infinity, a lone surrogate, nesting too deep
        return False
    return True


def _children(values: list[object]) -> Iterator[object]:
    for value in values:
        if isinstance(value, dict):
            yield from value.values()
        elif isinstance(value, list):
            yield from value


def _bus_text(entry: dict[str, Any]) -> str:
    payload = entry.get("payload")
    if isinstance(payload, dict):
        kind = payload.get("message_kind", "message")
        body = payload.get("body", "")
    else:
        kind, body = "message", payload
    return f"Message from {entry['received_from']} ({kind}): {body}"
