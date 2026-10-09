"""Test doubles for the gateway: a deterministic stub provider and an in-process transport.

No docker and no network: the stub replaces the only egress, and the transport drives a FastAPI app
in-process.
"""

from __future__ import annotations

import itertools
from typing import Any

from fastapi import FastAPI
from loc_arena.gateway.core import Message, ProviderError, ProviderResult, ToolSpec
from loc_arena.gateway.transport import Transport
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

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        self.inputs.append(joined_content(messages))
        self._n += 1
        return ProviderResult(text=f"out#{self._n}", prompt_tokens=self._pt, completion_tokens=self._ct)


class AppTransport(Transport):
    """A ``Transport`` that drives a FastAPI app in-process via Starlette's TestClient (no sockets)."""

    def __init__(self, app: FastAPI) -> None:
        self._client = TestClient(app)

    def post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.post(path, json=body)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result


class JsonVerdictProvider:
    """A deterministic provider returning a fixed JSON monitor verdict (live-monitor tests, no network)."""

    def __init__(self, suspicion: float = 0.9, reason: str = "stub verdict") -> None:
        self._text = f'{{"suspicion": {suspicion}, "reason": "{reason}"}}'

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        return ProviderResult(text=self._text, prompt_tokens=1, completion_tokens=1)


class MalformedProvider:
    """A provider that returns an unparseable completion (to exercise the monitor parse-error fallback)."""

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        return ProviderResult(
            text="I think this looks fine, no JSON here.",
            prompt_tokens=1,
            completion_tokens=1,
        )


class AlternatingVerdictProvider:
    """A provider whose replies alternate: unparseable first, then a JSON verdict, and so on."""

    def __init__(self) -> None:
        self._providers = itertools.cycle((MalformedProvider(), JsonVerdictProvider()))

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        return next(self._providers).generate(model, messages, temperature, max_tokens, tools)


class FailingProvider:
    """A dead provider: every call fails but those numbered in ``answered`` (from 1), which give a verdict.

    It records the model of every call it was sent.
    """

    def __init__(self, *, answered: frozenset[int] = frozenset()) -> None:
        self.models: list[str] = []
        self._answered = answered

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        self.models.append(model)
        if len(self.models) in self._answered:
            return JsonVerdictProvider().generate(model, messages, temperature, max_tokens, tools)
        raise ProviderError(f"call {len(self.models)} failed")
