"""Each MCP service's record of the tool calls made to it: one JSON line per call, in OpenTelemetry's names.

A service built on the official MCP SDK adds ``tool_call_recorder`` to its middleware. The line carries the
attributes the OpenTelemetry semantic conventions give an MCP tool call (``mcp.method.name``,
``jsonrpc.request.id``, ``mcp.session.id`` on a session, ``gen_ai.operation.name``, ``gen_ai.tool.name``,
``gen_ai.tool.call.arguments``, ``gen_ai.tool.call.result`` on success, ``error.type`` on failure) and the
calling agent as ``gen_ai.agent.name``, plus its ``time``. A service in a container writes it to stdout; one
an in-process run serves writes it to ``records/<service>.jsonl`` in the episode's directory, so both runs
leave the same lines. ``ToolRecord`` reads one back.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext
from mcp.shared.exceptions import MCPError
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

TOOL_CALL = "tools/call"
SESSION_HEADER = "mcp-session-id"  # a 2025-era session's id; a 2026-era call has none
Write = Callable[[str], None]


class ToolRecord(BaseModel):
    """One line of a service's record, read back: a tool call, the agent that made it, and its outcome."""

    model_config = ConfigDict(frozen=True)

    time: AwareDatetime  # a time without its UTC offset would fall in the wrong play window
    tool: str = Field(validation_alias="gen_ai.tool.name")
    arguments: dict[str, JsonValue] = Field(validation_alias="gen_ai.tool.call.arguments")
    result: dict[str, JsonValue] | None = Field(default=None, validation_alias="gen_ai.tool.call.result")
    error: str | None = Field(default=None, validation_alias="error.type")
    agent: str = Field(validation_alias="gen_ai.agent.name")


def tool_call_recorder(agent: Callable[[], str | None], write: Write) -> ServerMiddleware[Any]:
    """A middleware that ``write``s one line per tool call, naming the calling ``agent()``."""

    # The SDK's middleware protocol names its first parameter ``ctx``.
    async def record(ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        if ctx.method != TOOL_CALL:
            return await call_next(ctx)
        params = ctx.params or {}
        session = ctx.request.headers.get(SESSION_HEADER) if ctx.request is not None else None
        line = {
            "time": datetime.now(UTC).isoformat(),
            "mcp.method.name": ctx.method,
            **({"mcp.session.id": session} if session else {}),
            "jsonrpc.request.id": str(ctx.request_id),
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": params.get("name"),
            "gen_ai.tool.call.arguments": params.get("arguments", {}),
            "gen_ai.agent.name": agent(),
        }
        try:
            result = await call_next(ctx)
        except MCPError as error:
            write(json.dumps({**line, "error.type": str(error.error.code)}))
            raise
        reply = result if isinstance(result, dict) else {}  # call_next returns the finished wire form
        outcome = (
            {"error.type": "tool_error"}
            if reply.get("isError")
            else {"gen_ai.tool.call.result": reply.get("structuredContent")}
        )
        write(json.dumps({**line, **outcome}))
        return result

    return record


def append_to(path: Path) -> Write:
    """A ``Write`` that appends each line to ``path``, creating its directory with the first."""

    def write(line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as records:
            records.write(f"{line}\n")

    return write
