"""The model-call types the gateway's core, its edge and its clients share."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, model_validator

from loc_arena.logging_.events import fingerprint

Message = dict[str, Any]  # an OpenAI Chat Completions message, passed through verbatim
ToolSpec = dict[str, Any]  # an OpenAI Chat Completions tool definition, passed through verbatim


@dataclass(frozen=True)
class ProviderResult:
    """A model completion and the token accounting used by the batch quota.

    ``cached_tokens`` is how many ``prompt_tokens`` came from a cached prefix (0 when the provider omits it).
    """

    text: str
    prompt_tokens: int
    completion_tokens: int
    tool_calls: list[dict[str, Any]] | None = None
    cached_tokens: int = 0

    def assistant_message(self) -> Message:
        """The reply as an assistant message a caller can append to its history."""
        msg: Message = {"role": "assistant", "content": self.text}
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        return msg


class GenerateRequest(BaseModel):
    """A raw or sanctioned generate call arriving at the core.

    Carries exactly one of ``prompt`` (a single user message) or ``messages`` (a full chat history). ``tools``
    enables native tool calling and requires ``messages``.
    """

    prompt: str | None = None
    messages: list[Message] | None = None
    tools: list[ToolSpec] | None = None
    caller_identity: str
    role: str = "teacher"
    turn_token: str | None = None
    durable_credential: str | None = None
    origin_job_id: str | None = None
    monitor_credential: str | None = None
    temperature: float | None = None

    @model_validator(mode="after")
    def _one_input(self) -> GenerateRequest:
        if (self.prompt is None) == (self.messages is None):
            raise ValueError("GenerateRequest needs exactly one of prompt or messages")
        if self.tools and self.messages is None:
            raise ValueError("GenerateRequest tools require messages")
        return self

    def chat_messages(self) -> list[Message]:
        """The request as a chat message list (a ``prompt`` becomes one user message)."""
        if self.messages is not None:
            return self.messages
        return [{"role": "user", "content": self.prompt}]


def input_fingerprint(req: GenerateRequest) -> str:
    """The ``prompt_fp`` of a request, over what the caller submitted (pre-injection).

    A ``prompt`` fingerprints the bare string, so a self-feeding loop that passes the previous output as the
    next prompt links ``prompt_fp == output_fp`` (the verifier relies on this).
    """
    return fingerprint(req.prompt if req.prompt is not None else req.messages)


def output_fingerprint(req: GenerateRequest, text: str, tool_calls: list[dict[str, Any]] | None) -> str:
    """The ``output_fp`` of a reply: the bare text for a ``prompt`` request, else the assistant message."""
    if req.prompt is not None:
        return fingerprint(text)
    return fingerprint(ProviderResult(text, 0, 0, tool_calls).assistant_message())


class GenerateResponse(BaseModel):
    """The core's reply: the completion, the resolved identity/stamps, and the sealed seq it was written at.

    ``caller_identity`` and ``credential_kind`` are the RESOLVED values (a durable credential resolves to
    its account), so the tamperable edge can build a faithful mirror record from the reply without holding
    the secret. ``in_mirror`` tells the edge whether the deployed logging covers this call (else
    underreported_activity_logging drops
    it).
    """

    text: str
    turn_id: str | None
    batch_id: str | None
    seq: int
    caller_identity: str = ""
    credential_kind: str = "none"
    in_mirror: bool = True
    tool_calls: list[dict[str, Any]] | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0

    def assistant_message(self) -> Message:
        """The reply as an assistant message, matching the sealed ``output_fp`` for a chat request."""
        return ProviderResult(
            self.text,
            self.prompt_tokens,
            self.completion_tokens,
            self.tool_calls,
        ).assistant_message()
