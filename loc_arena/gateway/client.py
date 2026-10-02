"""The model clients of the runner's scaffold: the gateway edge over HTTP, and each agent's identity on it."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import httpx
from fastapi import HTTPException

from loc_arena.gateway.openrouter_provider import ProviderError
from loc_arena.stack.constants import BATCH_GENERATE_ROUTE, GENERATE_ROUTE
from loc_arena.stack.contracts import (
    BatchGenerateRequest,
    RelayedBatchGenerateResponse,
    RelayedGenerateResponse,
    Servable,
)
from loc_arena.stack.model_call import GenerateRequest, GenerateResponse, Message, ToolSpec
from loc_arena.stack.service_client import ServiceClient

GATEWAY_FAILURES: Final = (ProviderError, HTTPException, httpx.HTTPError)


class GatewayCallError(RuntimeError):
    """A model call through the gateway failed: the provider failed, or the gateway refused the call."""


class EdgeClient:
    """The gateway edge over HTTP (agent-net): what the runner's scaffold calls in the stack."""

    def __init__(self, client: ServiceClient) -> None:
        """Relay through ``client`` (the edge's base URL and the relay timeout already set)."""
        self._client = client

    def generate(self, request: GenerateRequest, /) -> RelayedGenerateResponse:
        """One model call; a refusal raises ``httpx.HTTPStatusError`` with the edge's status."""
        return self._client.post_model(GENERATE_ROUTE, request, RelayedGenerateResponse)

    def batch_generate(self, request: BatchGenerateRequest, /) -> RelayedBatchGenerateResponse:
        """One sanctioned batch; a refusal raises ``httpx.HTTPStatusError`` with the edge's status."""
        return self._client.post_model(BATCH_GENERATE_ROUTE, request, RelayedBatchGenerateResponse)


class GatewayClient:
    """One agent's model client: its caller identity, and the current sanctioned turn's token."""

    def __init__(self, gateway: Servable, caller_identity: str) -> None:
        """Call ``gateway`` as ``caller_identity``, with no turn token until the scaffold sets one."""
        self._gateway = gateway
        self._caller_identity = caller_identity
        self._turn_token: str | None = None

    def set_turn_token(self, token: str | None) -> None:
        """Set or clear the token for the current sanctioned turn (set by the scaffold at turn boundaries)."""
        self._turn_token = token

    def generate(self, prompt: str, *, role: str = "teacher") -> str:
        """Make one raw model call; it carries a turn token only inside a sanctioned turn."""
        request = GenerateRequest(
            prompt=prompt,
            caller_identity=self._caller_identity,
            role=role,
            turn_token=self._turn_token,
        )
        return self._gateway.generate(request).text

    def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        role: str = "teacher",
    ) -> GenerateResponse:
        """Make one model call over a full chat history; the reply carries text and any native tool calls."""
        request = GenerateRequest(
            messages=messages,
            tools=tools,
            caller_identity=self._caller_identity,
            role=role,
            turn_token=self._turn_token,
        )
        return self._gateway.generate(request)

    def batch_generate(self, prompts: Sequence[str], *, role: str = "teacher") -> list[str]:
        """Register a sanctioned, quota-limited batch and return one completion per prompt."""
        request = BatchGenerateRequest(
            prompts=tuple(prompts),
            caller_identity=self._caller_identity,
            role=role,
        )
        return list(self._gateway.batch_generate(request).texts)
