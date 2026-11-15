"""What a company service refuses, and what fails in it: told to its caller, and written to its own log.

A store refuses a request by raising a ``RefusalError``, with the HTTP status that says why. Over HTTP the
caller gets the status and the reason; over MCP, a tool error with the reason. Either way the service logs a
warning with both. A request its HTTP API fails on unexpectedly is a 500 and an error of its log, with the
exception; the MCP SDK logs a tool's own. A reason names what was asked for, never a body or a credential.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from http import HTTPStatus

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from mcp.server.mcpserver.exceptions import ToolError

_LOGGER = logging.getLogger(__name__)


class RefusalError(Exception):
    """A request a service refuses, with the HTTP status that says why."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        """Refuse with ``status`` and a message the caller reads."""
        super().__init__(message)
        self.status = status


def log_refusals_and_failures(app: FastAPI) -> None:
    """Make ``app`` answer a refusal with its status and reason, and a failure with a 500; log each."""

    @app.exception_handler(RefusalError)
    async def refuse(_: Request, refusal: RefusalError) -> Response:
        _log_refusal(refusal)
        return JSONResponse({"detail": str(refusal)}, refusal.status)

    @app.exception_handler(Exception)
    async def fail(request: Request, error: Exception) -> Response:
        _LOGGER.error("failed on %s %s", request.method, request.url.path, exc_info=error)
        return JSONResponse({"detail": "Internal Server Error"}, HTTPStatus.INTERNAL_SERVER_ERROR)


@contextlib.contextmanager
def refusals_as_tool_errors() -> Iterator[None]:
    """Raise a refusal as a tool error, logged: only a tool error's text reaches the caller."""
    try:
        yield
    except RefusalError as refusal:
        _log_refusal(refusal)
        raise ToolError(str(refusal)) from refusal


def _log_refusal(refusal: RefusalError) -> None:
    _LOGGER.warning("refused with %d: %r", refusal.status, str(refusal))  # %r: one line, whatever was sent
