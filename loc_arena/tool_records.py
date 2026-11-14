"""Each MCP service's record of the tool calls made to it: one JSON line per call, in OpenTelemetry's names.

A service built on the official MCP SDK adds ``tool_call_recorder`` to its middleware. The line carries the
attributes the OpenTelemetry semantic conventions give an MCP tool call (``mcp.method.name``,
``jsonrpc.request.id``, ``mcp.session.id`` on a session, ``gen_ai.operation.name``, ``gen_ai.tool.name``,
``gen_ai.tool.call.arguments``, ``gen_ai.tool.call.result`` on success, ``error.type`` on failure) and the
calling agent as ``gen_ai.agent.name``, plus its ``time``. A failure also carries the error's text as
``otel.status_description``, the span status description OpenTelemetry exports to formats outside OTLP, which
the MCP conventions fill with the error's message. A service in a container writes it to stdout, and to its
file on the records volume, which the episode reads during play (``loc_arena.notices``); the workspace
service, which the episode serves, writes it to ``records/<service>.jsonl`` in the play's directory.
``ToolRecord`` reads one back.

A call whose work goes on whatever becomes of the call, as a deployment's hand-over does, also writes its line
as that work starts (``record_started``), marked ``tool.call.started``, with what is known then as its result:
a record read before the call returns, or after a call that never did, holds the work.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext
from mcp.shared.exceptions import MCPError
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

TOOL_CALL = "tools/call"
SESSION_HEADER = "mcp-session-id"  # a 2025-era session's id; a 2026-era call has none
STARTED = "tool.call.started"  # on the line a call writes as its work starts, before the line of its end
Write = Callable[[str], None]
# How the tool call being served writes its line with a result so far; none outside a recorded call.
_write_started: ContextVar[Callable[[dict[str, JsonValue]], None] | None] = ContextVar(
    "write_started",
    default=None,
)


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
    started: bool = Field(default=False, validation_alias=STARTED)  # the call's own line follows at its end


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

        def write_line(outcome: Mapping[str, JsonValue]) -> None:
            write(json.dumps({**line, **outcome, **(annotate() if annotate else {})}))

        _write_started.set(lambda known: write_line({"gen_ai.tool.call.result": known, STARTED: True}))
        try:
            result = await call_next(ctx)
        except MCPError as error:
            write_line({"error.type": str(error.error.code), "otel.status_description": error.error.message})
            raise
        reply = result if isinstance(result, dict) else {}  # call_next returns the finished wire form
        text = " ".join(block["text"] for block in reply.get("content", []) if block.get("type") == "text")
        write_line(
            {"error.type": "tool_error", "otel.status_description": text}
            if reply.get("isError")
            else {"gen_ai.tool.call.result": reply.get("structuredContent")},
        )
        return result

    return record


def record_started(result: dict[str, JsonValue]) -> None:
    """Write the line of the tool call being served with ``result`` so far: its work has started.

    For work that goes on whatever becomes of the call: the record holds it from here, and the call's own
    line, written as it returns, says how it ended. Outside a recorded call, nothing is written.
    """
    if (write_started := _write_started.get()) is not None:
        write_started(result)


def record_writer(directory: Path, service: str) -> Write:
    """A ``Write`` of ``service``'s record: each line whole on stdout, and in its file of ``directory``.

    The file is ``<service>.jsonl``; a service started where no ``directory`` is mounted writes stdout alone.
    """
    path = directory / f"{service}.jsonl" if directory.is_dir() else None
    lock = threading.Lock()  # request handlers and a service's own threads each write whole lines

    def write(line: str) -> None:
        with lock:
            print(line, flush=True)  # noqa: T201 - the record a stack run reads from the container's log
            if path is None:
                return
            try:
                with path.open("a", encoding="utf-8") as records:
                    records.write(f"{line}\n")
            except OSError as error:  # its call was made: the line is on stdout, and makes no notice
                print(f"record not written to {path}: {error}", file=sys.stderr, flush=True)  # noqa: T201

    return write


def append_to(path: Path) -> Write:
    """A ``Write`` that appends each line to ``path``, creating its directory with the first."""

    def write(line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as records:
            records.write(f"{line}\n")

    return write
