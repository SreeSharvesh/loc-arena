"""Each agent's MCP client: the tools its server offers it, and calls to them, on the official MCP SDK.

In a stack run the server is agentgateway's MCP route, reached with the agent's key, which offers each agent
only its tools; in an in-process run it is a server object, reached in memory. Each use opens a connection and
closes it, so the synchronous agent loop holds no event loop between calls. A call that fails to reach its
server is an error result for the agent, never an exception for the episode.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Collection, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent, Tool
from pydantic import JsonValue
from tenacity import retry, retry_if_exception_type, stop_after_delay, wait_exponential

from loc_arena.gateway.core import ToolSpec

_LOGGER = logging.getLogger(__name__)

Connect = Callable[[], AbstractAsyncContextManager[Client]]


@asynccontextmanager
async def over_http(url: str, key: str, timeout_seconds: float) -> AsyncIterator[Client]:
    """A client of the MCP server at ``url``, ``key`` its bearer token, ``timeout_seconds`` per request."""
    headers = {"Authorization": f"Bearer {key}"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout_seconds) as http,
        Client(streamable_http_client(url, http_client=http)) as client,
    ):
        yield client


class McpTools:
    """One agent's MCP tools: listed once from its server, each call over a fresh connection."""

    def __init__(
        self,
        connect: Connect,
        *,
        connect_seconds: float = 0.0,
        granted: Collection[str] | None = None,
    ) -> None:
        """Reach the server with ``connect``; retry the first listing for up to ``connect_seconds``.

        With ``granted``, only those of the server's tools are offered: a server that offers every agent all
        its tools, as an in-process one does, is narrowed to the agent's ``sandbox.tools``.
        """
        self._connect = connect
        self._connect_seconds = connect_seconds
        self._granted = granted
        self._specs: list[ToolSpec] | None = None

    def specs(self) -> list[ToolSpec]:
        """The tools the server offers this agent, as the function schemas the model is given."""
        if self._specs is None:
            listed = retry(
                retry=retry_if_exception_type(Exception),
                stop=stop_after_delay(self._connect_seconds),
                wait=wait_exponential(max=2),
                reraise=True,
            )(anyio.run)(self._list)
            self._specs = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": tool.input_schema,
                    },
                }
                for tool in listed
            ]
        return self._specs

    def names(self) -> set[str]:
        """The names of the tools the server offers this agent."""
        return {spec["function"]["name"] for spec in self.specs()}

    def call(self, tool: str, arguments: Mapping[str, JsonValue]) -> dict[str, Any]:
        """Call ``tool``: its structured result, or an error result saying what went wrong."""
        try:
            return anyio.run(self._call, tool, arguments)
        except Exception as error:  # noqa: BLE001 - a refused or broken connection is the agent's error result
            _LOGGER.warning("%s could not be called: %r", tool, error)
            return {"error": f"{tool} could not be called: the tool server is unreachable", "tool": tool}

    async def _list(self) -> list[Tool]:
        async with self._connect() as client:
            tools = (await client.list_tools()).tools
        return [tool for tool in tools if self._granted is None or tool.name in self._granted]

    async def _call(self, tool: str, arguments: Mapping[str, JsonValue]) -> dict[str, Any]:
        async with self._connect() as client:
            result = await client.call_tool(tool, dict(arguments))
        if result.is_error or result.structured_content is None:
            text = " ".join(block.text for block in result.content if isinstance(block, TextContent))
            return {"error": text, "tool": tool}
        return dict(result.structured_content)
