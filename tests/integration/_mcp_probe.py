"""A tool call over MCP from inside a container of the engine image: the head of a ``python -c`` script.

It defines ``call_tool(url, token, tool, arguments)``, which gives the call's structured result, or
``{"error": ...}`` when the server answered with an error.
"""

CALL_TOOL = """
import anyio, httpx2, json, sys
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
async def _call(url, token, tool, arguments):
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=30) as http,
        Client(streamable_http_client(url, http_client=http)) as client,
    ):
        result = await client.call_tool(tool, arguments)
    if result.is_error or result.structured_content is None:
        text = " ".join(block.text for block in result.content if isinstance(block, TextContent))
        return {"error": text, "tool": tool}
    return dict(result.structured_content)
def call_tool(url, token, tool, arguments):
    return anyio.run(_call, url, token, tool, arguments)
"""
