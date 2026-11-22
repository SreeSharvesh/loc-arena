"""The text of the chat requests and replies in the gateway's call record.

A reply's text includes the tool calls it makes, rendered as the next request replays them, so the loop finder
sees a call carry an earlier one's output forward even when a turn says nothing but its calls.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field, JsonValue, ValidationError


class _TextPart(BaseModel):
    text: str = ""


class _Function(BaseModel):
    name: str = ""
    arguments: str = ""


class _ToolCall(BaseModel):
    index: int | None = None  # a streamed fragment's: the call it continues
    function: _Function = Field(default_factory=_Function)


class _Message(BaseModel):
    content: str | list[_TextPart] | None = None
    tool_calls: list[_ToolCall] | None = None  # the chat schema lets a message without calls say null

    def read_content(self) -> str:
        if isinstance(self.content, list):
            return "".join(part.text for part in self.content)
        return self.content or ""

    def read_text(self) -> str:
        """Its content and its tool calls, as ``read_reply`` renders a reply."""
        calls = [(call.function.name, call.function.arguments) for call in self.tool_calls or []]
        return _render_message(self.read_content(), calls)


class _ChatRequest(BaseModel):
    messages: list[_Message] = []


class _Choice(BaseModel):
    message: _Message | None = None
    delta: _Message | None = None  # a streamed chunk's


class _ChatReply(BaseModel):
    choices: list[_Choice] = []


def read_prompt(request: JsonValue) -> str:
    """The text of a chat request's messages, in order; empty for a body that is no chat request."""
    try:
        return "\n".join(message.read_text() for message in _ChatRequest.model_validate(request).messages)
    except ValidationError:
        return ""


def _read_replies(response: JsonValue) -> list[JsonValue]:
    """A chat reply's JSON bodies: a stream's (SSE) chunks, or the one body."""
    if isinstance(response, str):
        chunks = [line.removeprefix("data:").strip() for line in response.splitlines()]
        return [json.loads(chunk) for chunk in chunks if _is_json_object(chunk)]
    return [response]


def read_response_id(response: JsonValue) -> str | None:
    """The provider's id of a chat reply, as its first body or chunk gives it; none when it gives none."""
    replies = _read_replies(response)
    response_id = replies[0].get("id") if replies and isinstance(replies[0], dict) else None
    return response_id if isinstance(response_id, str) else None


def read_reply(response: JsonValue) -> str:
    """The text of a chat reply's first choice and its tool calls, a streamed (SSE) one reassembled.

    Empty for any other body: one the gateway cut at its limit no longer parses.
    """
    content = []
    calls: dict[int, tuple[str, str]] = {}
    for reply in _read_replies(response):
        try:
            choices = _ChatReply.model_validate(reply).choices
        except ValidationError:
            return ""
        part = (choices[0].message or choices[0].delta) if choices else None
        if part is None:
            continue
        content.append(part.read_content())
        for position, call in enumerate(part.tool_calls or []):
            key = position if call.index is None else call.index
            name, arguments = calls.get(key, ("", ""))
            calls[key] = (name or call.function.name, arguments + call.function.arguments)
    return _render_message("".join(content), [calls[key] for key in sorted(calls)])


def _render_message(content: str, calls: list[tuple[str, str]]) -> str:
    """A message as text: its content, then each tool call it makes, a line each."""
    lines = [content, *(_render_call(name, arguments) for name, arguments in calls)]
    return "\n".join(line for line in lines if line)


def _render_call(name: str, arguments: str) -> str:
    """A tool call as text, its arguments re-serialized canonically.

    The next request replays the call under a new id and may re-serialize its arguments, so only canonical
    arguments let the reply be found in that request's prompt.
    """
    try:
        arguments = json.dumps(json.loads(arguments), sort_keys=True, separators=(",", ":"))
    except ValueError:  # not JSON, as when cut off mid-stream
        pass
    return f"{name}({arguments})"


def _is_json_object(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False
