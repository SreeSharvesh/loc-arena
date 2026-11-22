"""A tool call over MCP from inside a container of the engine image: the head of a ``python -c`` script.

It defines ``call_tool(url, token, tool, arguments)``, which gives the call's structured result, or
``{"error": ...}`` when the server answered with an error. ``ToolCaller`` is one process of it that makes
every call asked of it.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from typing import IO, Any

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
# The process kept: each line of its input is a call's url, token, tool and arguments, and its result a line.
_SERVE_CALLS = (
    CALL_TOOL
    + """
for line in sys.stdin:
    print(json.dumps(call_tool(*json.loads(line))), flush=True)
"""
)


class ToolCaller:
    """A process in a container of the engine image that makes every tool call asked of it.

    A call run as a process of its own spends over half a second importing the MCP client before it calls;
    this process imports it once. Each call is still a session of its own. A call that raises ends the
    process, as it would end its own, and the next call starts another, as does the first.
    """

    def __init__(self, enter: Sequence[str], environment: Mapping[str, str]) -> None:
        """``enter`` runs a command in the container, as ``docker compose exec -T iam`` does."""
        self._command = [*enter, "python", "-u", "-c", _SERVE_CALLS]
        self._environment = environment
        self._process: subprocess.Popen[str] | None = None
        self._errors: IO[str] | None = None

    def call(self, url: str, token: str, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """The result of ``tool`` called at ``url`` with ``token``."""
        if self._process is None or self._process.poll() is not None:
            self._errors = tempfile.TemporaryFile("w+")
            self._process = subprocess.Popen(
                self._command,
                env=self._environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._errors,
                text=True,
            )
        assert self._process.stdin and self._process.stdout and self._errors
        self._process.stdin.write(f"{json.dumps([url, token, tool, arguments])}\n")
        self._process.stdin.flush()
        answered = self._process.stdout.readline()
        if not answered:
            self._process.wait()
            self._errors.seek(0)
            raise AssertionError(f"the call of {tool} ended its process: {self._errors.read()[-2000:]}")
        return json.loads(answered)
