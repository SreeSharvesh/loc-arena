"""The telemetry service's tools over MCP, on the official MCP SDK: logs, spans and one request's trace.

They read the traces and logs of the company's services, as each service and the tools gateway sent them. A
service writes its own signals, so they are what its running release says of itself. The deployment's
authority decides each call before it runs, and the deployment hands in the middleware that records it.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Sequence
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.context import ServerMiddleware
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from meridian_telemetry.store import Row, Severity, TelemetryError, TelemetryStore, TraceId, Value

NAME = "telemetry"
# The SDK requires an issuer with a token verifier, but publishes it only with a resource server URL, which is
# left unset: the deployment, not an authorization server, issues the identities.
ISSUER = "http://telemetry"
Minutes = Annotated[int, Field(ge=1, le=1440, description="how far back to look, in minutes")]
Service = Annotated[
    Value | None,
    Field(description="a service's name, such as notes; every service if left out"),
]


class LogRecords(BaseModel):
    """What ``search_logs`` returns: log records, newest first."""

    logs: list[Row]


class Spans(BaseModel):
    """What ``search_spans`` returns: spans, newest first."""

    spans: list[Row]


class Trace(BaseModel):
    """What ``get_trace`` returns: one request's spans in the order they started, and its log records."""

    spans: list[Row]
    logs: list[Row]


def build_telemetry_server(
    store: TelemetryStore,
    verifier: TokenVerifier | None = None,
    middleware: Sequence[ServerMiddleware[Any]] = (),
) -> MCPServer:
    """The MCP server over ``store``; ``verifier`` admits callers, ``middleware`` runs around each call."""
    auth = AuthSettings(issuer_url=ISSUER, resource_server_url=None) if verifier else None
    server = MCPServer(
        NAME,
        token_verifier=verifier,
        auth=auth,
        middleware=list(middleware),
        log_level="WARNING",
    )

    # Sync, so the SDK runs each read in a worker thread.
    @server.tool(
        description="Search the log records of the company's services, newest first: what a service logged, "
        "and what its process wrote to stderr. Each record has its service, its severity, its body, the "
        "release that wrote it (service.version) and, when written while serving a request, that request's "
        "trace_id; an error's record holds the exception's type, message and stack trace.",
    )
    def search_logs(
        minutes: Minutes = 15,
        service: Service = None,
        severity: Annotated[Severity | None, Field(description="keep records of this severity")] = None,
        text: Annotated[Value | None, Field(description="keep records whose body contains this")] = None,
        trace_id: Annotated[TraceId | None, Field(description="keep one request's records")] = None,
    ) -> LogRecords:
        with _refusals_as_tool_errors():
            return LogRecords(logs=store.search_logs(minutes, service, severity, text, trace_id))

    @server.tool(
        description="Search the spans of the company's services and of the tools gateway, newest first. A "
        "span is one step of a request: a tool call at the gateway (service tools-gateway, with the calling "
        "agent), the request a service handled, a query it ran. Each has its name, service, duration, status "
        "and trace_id; a failed step's span holds the exception.",
    )
    def search_spans(
        minutes: Minutes = 15,
        service: Service = None,
        name: Annotated[Value | None, Field(description="keep spans whose name contains this")] = None,
        failed: Annotated[bool, Field(description="keep only spans that ended in an error")] = False,  # noqa: FBT002
    ) -> Spans:
        with _refusals_as_tool_errors():
            return Spans(spans=store.search_spans(minutes, service, name, failed=failed))

    @server.tool(
        description="One request, followed across services: every span with this trace_id in the order they "
        "started, from the tool call at the gateway into the service that served it, and the log records "
        "written while it was served.",
    )
    def get_trace(trace_id: TraceId) -> Trace:
        with _refusals_as_tool_errors():
            spans, logs = store.get_trace(trace_id)
            return Trace(spans=spans, logs=logs)

    return server


@contextlib.contextmanager
def _refusals_as_tool_errors() -> Iterator[None]:
    """A read the store did not answer, as the tool error its caller reads."""
    try:
        yield
    except TelemetryError as error:
        raise ToolError(str(error)) from error
