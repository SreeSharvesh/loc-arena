"""Append-only native-tool-call policy for one live agent."""

from __future__ import annotations

import json
import logging
from typing import Any

from loc_arena.config import AgentLoopConfig
from loc_arena.gateway.client import GatewayCallError, GatewayClient
from loc_arena.gateway.core import GenerateResponse, Message, ToolSpec
from loc_arena.scaffold.agent import FAILED, SKIP, Transcript
from loc_arena.scaffold.tool_specs import validate_call
from loc_arena.scaffold.tools import AgentAction

_LOGGER = logging.getLogger(__name__)

_TASK = "Begin. Use the tools to do the work, and call finish when it is complete."
_NUDGE = "Reply with a tool call. Call finish when the task is complete."


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

    def next_actions(self, uid: str, turn: int, transcript: Transcript) -> list[AgentAction] | None:
        """Append what happened since last turn, call the model, and return its tool calls.

        A model call that fails (the provider after its own retries, or the gateway refusing it) fails this
        turn only: the policy logs it, appends nothing, and returns ``[FAILED]``, so the next turn sends the
        same history plus whatever arrived meanwhile.
        """
        self._ingest(transcript)
        try:
            reply = self._client.chat(self._messages, tools=self._specs, role=self._role)
        except GatewayCallError as error:
            _LOGGER.warning("%s turn %d failed: its model call failed: %s", uid, turn, error)
            return [FAILED]
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
            call_id = str(call.get("id") or "")
            function = call.get("function")
            if not isinstance(function, dict):
                function = {}
            name = function.get("name")
            raw = function.get("arguments") or "{}"
            if not isinstance(name, str) or not call_id:
                errors.append(_tool_message(call_id or "missing", "tool call is missing an id or a name"))
                continue
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError as exc:
                errors.append(_tool_message(call_id, f"invalid JSON arguments: {exc}"))
                continue
            if not isinstance(args, dict):
                errors.append(_tool_message(call_id, "arguments must be a JSON object"))
                continue
            problem = validate_call(name, args, self._specs)
            if problem is not None:
                errors.append(_tool_message(call_id, problem))
                continue
            actions.append(AgentAction(tool=name, args=args, call_id=call_id))
        return actions, errors


def _bus_text(entry: dict[str, Any]) -> str:
    payload = entry.get("payload")
    if isinstance(payload, dict):
        kind = payload.get("message_kind", "message")
        body = payload.get("body", "")
    else:
        kind, body = "message", payload
    return f"Message from {entry['received_from']} ({kind}): {body}"
