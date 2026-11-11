"""Each MCP service's record of the tool calls made to it: one JSON line per call, in OpenTelemetry's names.

A service built on the official MCP SDK adds ``tool_call_recorder`` to its middleware. The line carries the
attributes the OpenTelemetry semantic conventions give an MCP tool call (``mcp.method.name``,
``jsonrpc.request.id``, ``mcp.session.id`` on a session, ``gen_ai.operation.name``, ``gen_ai.tool.name``,
``gen_ai.tool.call.arguments``, ``gen_ai.tool.call.result`` on success, ``error.type`` on failure) and the
calling agent as ``gen_ai.agent.name``, plus its ``time``. A failure also carries the error's text as
``otel.status_description``, the span status description OpenTelemetry exports to formats outside OTLP, which
the MCP conventions fill with the error's message. A service in a container writes it to stdout, and to its
file on the records volume, which the episode reads during play (``loc_arena.notices``); one an in-process run
serves writes it to ``records/<service>.jsonl`` in the episode's directory, so both runs leave the same lines.
``ToolRecord`` reads one back.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
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
    status_description: str | None = Field(default=None, validation_alias="otel.status_description")
    agent: str = Field(validation_alias="gen_ai.agent.name")
    iam_decision: str | None = Field(default=None, validation_alias="iam.decision.id")


def tool_call_recorder(
    agent: Callable[[], str | None],
    write: Write,
    annotate: Callable[[], Mapping[str, str]] | None = None,
) -> ServerMiddleware[Any]:
    """A middleware that ``write``s one line per tool call, naming the calling ``agent()``.

    ``annotate`` adds attributes known only once the call has run, such as the IAM decision it was made under.
    """

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
            failure = {"error.type": str(error.error.code), "otel.status_description": error.error.message}
            write(json.dumps({**line, **failure, **(annotate() if annotate else {})}))
            raise
        reply = result if isinstance(result, dict) else {}  # call_next returns the finished wire form
        text = " ".join(block["text"] for block in reply.get("content", []) if block.get("type") == "text")
        outcome = (
            {"error.type": "tool_error", "otel.status_description": text}
            if reply.get("isError")
            else {"gen_ai.tool.call.result": reply.get("structuredContent")}
        )
        write(json.dumps({**line, **outcome, **(annotate() if annotate else {})}))
        return result

    return record


def record_writer(directory: Path, service: str) -> Write:
    """A ``Write`` of ``service``'s record: each line whole on stdout, and in its file of ``directory``.

    The file is ``<service>.jsonl``; a service started where no ``directory`` is mounted writes stdout alone.
    """
    path = directory / f"{service}.jsonl" if directory.is_dir() else None
    lock = threading.Lock()  # request handlers and a service's own threads each write whole lines

    def write(line: str) -> None:
        with lock:
            print(line, flush=True)  # noqa: T201 - the record a stack run reads from the container's log
            if path is not None:
                with path.open("a", encoding="utf-8") as records:
                    records.write(f"{line}\n")

    return write


def append_to(path: Path) -> Write:
    """A ``Write`` that appends each line to ``path``, creating its directory with the first."""

    def write(line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as records:
            records.write(f"{line}\n")

    return write
