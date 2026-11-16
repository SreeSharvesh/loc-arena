"""The audit record of a company HTTP service: one JSON line per request it answers.

A line holds the request's time, its caller, the container it came from, its method, path and status, the
central authorization decision it was made under, if any, and, for an operation of the service's API, the
operation's name, its arguments and what it created. A route names these on ``request.state``: ``operation``,
``arguments``, ``outcome`` and ``caller``, or a central authority's ``iam_operation``, ``iam_principal`` and
``iam_decision_id``.
"""

from __future__ import annotations

import functools
import json
import socket
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import RequestResponseEndpoint


def record_each_request(app: FastAPI, write_audit_line: Callable[[str], None]) -> None:
    """Make ``app`` write the audit line of each request it answers."""

    @app.middleware("http")
    async def write_audit_line_for_each_request(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        response = await call_next(request)
        operation = getattr(request.state, "operation", None) or getattr(request.state, "iam_operation", None)
        arguments = getattr(request.state, "arguments", dict(request.path_params))
        caller = getattr(request.state, "caller", None) or getattr(request.state, "iam_principal", None)
        decision = getattr(request.state, "iam_decision_id", None)
        outcome = getattr(request.state, "outcome", None)
        audit_line = {
            "wall_ts": datetime.now(UTC).isoformat(),
            "caller": caller,
            "container": _name_container(request.client.host) if request.client else None,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            **({"decision": decision} if decision else {}),
            **({"operation": operation, "arguments": arguments} if operation else {}),
            **({"outcome": outcome} if outcome else {}),
        }
        write_audit_line(json.dumps(audit_line))
        return response


@functools.cache
def _name_container(address: str) -> str:
    """The container at ``address``: its name on the network, else the address itself."""
    try:
        return socket.gethostbyaddr(address)[0]
    except OSError:
        return address
