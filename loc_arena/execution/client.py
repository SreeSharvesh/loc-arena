"""The runner's client of one agent's sandbox: a ``CodeToolRunner`` over the execution app's routes.

The sandbox belongs to the agent: its code runs as the app's own user, so it can replace the app with a
server of its own. Every call is therefore bounded as a whole, and every reply is untrusted:

- httpx's timeouts bound each phase and each read ("the maximum duration to wait for a chunk of data to be
  received", python-httpx.org/advanced/timeouts), so a server that trickles its headers or its body one
  byte at a time outlasts them. Each call instead runs under ``asyncio.timeout(reply_timeout_seconds)``,
  which cancels it, connecting, sending and reading the whole reply included. httpcore closes a connection
  whose exchange a cancellation interrupts (its ``except BaseException`` path, shielded from the
  cancellation), and each call's client and event loop close with it (``asyncio.run``), so no connection,
  task or thread outlives the call.
- The reply is read raw, never decoded (decoding would expand a compressed body past the cap), and refused
  past ``max_response_bytes``; it is then validated as a ``CodeToolResult``.

A sandbox that fails, stalls or answers badly gives the agent an error result and never crashes the runner.
``asyncio.run`` refuses a thread whose event loop is running, so the runner calls this client from
synchronous code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Self

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
        """Talk to the sandbox at ``base_url`` for the episode ``handle``, each call bounded by ``settings``.

        ``open_transport`` makes the transport of each call's client (tests pass an in-process one).
        """
        self._base_url = base_url
        self._handle = handle
        self._settings = settings
        self._open_transport = open_transport

    @classmethod
    def connect(cls, base_url: str, handle: EpisodeHandle, settings: ExecutionSettings) -> Self:
        """A client of the sandbox at ``base_url`` over HTTP, its calls capped and timed by ``settings``."""
        return cls(base_url, handle, settings)

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

    def close(self) -> None:
        """Release the client; nothing stays open between calls, each opens and closes its own connection."""

    def _post(self, route: str, body: ContractModel) -> bytes:
        """POST ``body`` and return the reply's body; raises ``httpx.HTTPError`` unless it is a timely 2xx."""
        return asyncio.run(self._post_within_deadline(route, body))

    async def _post_within_deadline(self, route: str, body: ContractModel) -> bytes:
        deadline = self._settings.reply_timeout_seconds
        async with httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._open_transport(),
            timeout=None,  # the deadline bounds the whole call; a per-read timeout adds nothing under it
            trust_env=False,  # no proxy, and no ~/.netrc credentials sent to a server the agent may run
        ) as client:
            try:
                async with asyncio.timeout(deadline):
                    return await self._read_reply(client, route, body)
            except TimeoutError as error:
                message = f"no complete reply within the {deadline:g} s deadline"
                raise httpx.TimeoutException(message) from error

    async def _read_reply(self, client: httpx.AsyncClient, route: str, body: ContractModel) -> bytes:
        """The body of a 2xx reply, read raw and refused past the cap; any other status raises."""
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
