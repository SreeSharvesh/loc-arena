"""Test doubles for the gateway: a deterministic stub provider and an in-process transport.

No docker and no network: the stub replaces the only egress, and the transport drives a FastAPI app
in-process.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from loc_arena.gateway.core import ProviderResult
from loc_arena.gateway.transport import Transport


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

    def generate(self, model: str, prompt: str, temperature: float, max_tokens: int) -> ProviderResult:
        self.inputs.append(prompt)
        self._n += 1
        return ProviderResult(text=f"out#{self._n}", prompt_tokens=self._pt, completion_tokens=self._ct)


class AppTransport(Transport):
    """A ``Transport`` that drives a FastAPI app in-process via Starlette's TestClient (no sockets)."""

    def __init__(self, app: FastAPI) -> None:
        from starlette.testclient import TestClient

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

    def generate(self, model: str, prompt: str, temperature: float, max_tokens: int) -> ProviderResult:
        return ProviderResult(text=self._text, prompt_tokens=1, completion_tokens=1)


class MalformedProvider:
    """A provider that returns an unparseable completion (to exercise the monitor parse-error fallback)."""

    def generate(self, model: str, prompt: str, temperature: float, max_tokens: int) -> ProviderResult:
        return ProviderResult(
            text="I think this looks fine, no JSON here.", prompt_tokens=1, completion_tokens=1
        )
