"""Test doubles for the gateway: a deterministic stub provider and an in-process transport.

No docker and no network: the stub replaces the only egress, and the transport drives a FastAPI app
in-process.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI
from loc_arena.gateway.core import ChatBody, Message, completion_body
from loc_arena.gateway.transport import TransportReply
from starlette.testclient import TestClient


def joined_content(messages: list[Message]) -> str:
    """The model input as one string; for a ``prompt`` request this is exactly the (injected) prompt."""
    return "\n\n".join(str(m["content"]) for m in messages)


class StubProvider:
    """A deterministic provider recording every model input and returning a unique completion per call.

    Unique outputs let a worker feed a call's output back as the next call's prompt to build a real
    self-feeding chain (whose fingerprints link), while distinct completions keep fingerprints distinct.
    """

    def __init__(self, *, prompt_tokens: int = 10, completion_tokens: int = 10) -> None:
        self.inputs: list[str] = []
        self._pt = prompt_tokens
        self._ct = completion_tokens
        self._n = 0

    def complete(self, body: ChatBody) -> ChatBody:
        self.inputs.append(joined_content(body["messages"]))
        self._n += 1
        return completion_body(f"out#{self._n}", self._pt, self._ct)


class AppTransport:
    """A ``Transport`` that drives a FastAPI app in-process via Starlette's TestClient (no sockets)."""

    def __init__(self, app: FastAPI) -> None:
        self._client = TestClient(app)

    def exchange(
        self,
        path: str,
        body: dict[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> TransportReply:
        resp = self._client.post(path, json=body, headers=dict(headers) if headers else None)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return TransportReply(result, resp.headers)

    def post_json(
        self,
        path: str,
        body: dict[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        return self.exchange(path, body, headers).body


class JsonVerdictProvider:
    """A deterministic provider returning a fixed JSON monitor verdict (live-monitor tests, no network)."""

    def __init__(self, suspicion: float = 0.9, reason: str = "stub verdict") -> None:
        self._text = f'{{"suspicion": {suspicion}, "reason": "{reason}"}}'

    def complete(self, body: ChatBody) -> ChatBody:
        return completion_body(self._text, 1, 1)


class MalformedProvider:
    """A provider that returns an unparseable completion (to exercise the monitor parse-error fallback)."""

    def complete(self, body: ChatBody) -> ChatBody:
        return completion_body("I think this looks fine, no JSON here.", 1, 1)


class AlternatingVerdictProvider:
    """A provider whose replies alternate: unparseable first, then a JSON verdict, and so on."""

    def __init__(self) -> None:
        self._providers = itertools.cycle((MalformedProvider(), JsonVerdictProvider()))

    def complete(self, body: ChatBody) -> ChatBody:
        return next(self._providers).complete(body)
