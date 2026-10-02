"""The runner's client of one agent's sandbox: a ``CodeToolRunner`` over the execution app's routes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
from pydantic import ValidationError

from loc_arena.stack.constants import TOOL_CALLS_ROUTE, WORKSPACES_ROUTE
from loc_arena.stack.contracts import (
    CodeToolCall,
    CodeToolResult,
    ContractModel,
    EpisodeHandle,
    WorkspaceCreate,
)
from loc_arena.stack.settings import ExecutionSettings

TransportFactory = Callable[[], httpx.AsyncBaseTransport]


class ReplyRefused(httpx.TransportError):
    """A sandbox's reply was refused: larger than the cap, or content-encoded."""


class ExecutionClient:
    """Runs one agent's code tools in that agent's sandbox for one episode: a ``CodeToolRunner``."""

    def __init__(
        self,
        base_url: str,
        handle: EpisodeHandle,
        settings: ExecutionSettings,
        *,
        open_transport: TransportFactory = httpx.AsyncHTTPTransport,
    ) -> None:
        """Talk to the sandbox at ``base_url`` for episode ``handle``, each call bounded by ``settings``."""
        self._base_url = base_url
        self._handle = handle
        self._settings = settings
        self._open_transport = open_transport

    def open_workspace(self) -> None:
        """Open the sandbox for this episode; raises ``httpx.HTTPError`` when it cannot (a broken stack)."""
        self._post(WORKSPACES_ROUTE, WorkspaceCreate(handle=self._handle))

    def run(self, call: CodeToolCall, /) -> CodeToolResult:
        """Run ``call`` in the sandbox; a failed, late, oversized or invalid reply is an error result."""
        route = TOOL_CALLS_ROUTE.format(handle=self._handle)
        try:
            return CodeToolResult.model_validate_json(self._post(route, call))
        except (httpx.HTTPError, ValidationError) as error:
            return CodeToolResult(
                result={"error": f"the sandbox gave no valid result: {error}", "tool": call.tool},
            )

    def _post(self, route: str, body: ContractModel) -> bytes:
        return asyncio.run(self._post_within_deadline(route, body))

    # One deadline per call and a raw, capped read: see docs/isolation/design.md#execution-client.
    async def _post_within_deadline(self, route: str, body: ContractModel) -> bytes:
        deadline = self._settings.reply_timeout_seconds
        async with httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._open_transport(),
            timeout=None,  # asyncio.timeout below bounds the whole call
            trust_env=False,  # no proxy, and no ~/.netrc credentials sent to a server the agent may run
        ) as client:
            try:
                async with asyncio.timeout(deadline):
                    return await self._read_reply(client, route, body)
            except TimeoutError as error:
                message = f"no complete reply within the {deadline:g} s deadline"
                raise httpx.TimeoutException(message) from error

    async def _read_reply(self, client: httpx.AsyncClient, route: str, body: ContractModel) -> bytes:
        content = body.model_dump_json()
        headers = {"Content-Type": "application/json"}
        async with client.stream("POST", route, content=content, headers=headers) as response:
            response.raise_for_status()
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise ReplyRefused("the reply is content-encoded: its decoded size cannot be capped")
            reply = bytearray()
            async for chunk in response.aiter_raw():
                reply += chunk
                if len(reply) > self._settings.max_response_bytes:
                    raise ReplyRefused(f"the reply exceeds {self._settings.max_response_bytes} bytes")
        return bytes(reply)
