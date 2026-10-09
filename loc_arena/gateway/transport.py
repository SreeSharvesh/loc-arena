"""HTTP transport shared by the edge and the client.

Enforces nothing on its own; it is the plumbing the tamperable edge uses to relay to the sealed core and
the in-sandbox client uses to reach the edge. A ``Transport`` is a single ``post_json`` call, so tests can
inject an in-process transport (over the FastAPI app) in place of a real network client.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx2


@dataclass(frozen=True)
class TransportReply:
    """A JSON reply plus the response headers.

    Headers carry the core's stamps (sequence, turn, credential). The edge reads them to write the mirror
    record and does not forward them to the caller.
    """

    body: dict[str, Any]
    headers: Mapping[str, str] = field(default_factory=dict)


class Transport(Protocol):
    """Post a JSON body to a path and return the parsed JSON reply."""

    def post_json(
        self,
        path: str,
        body: dict[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Send ``body`` to ``path`` and return the decoded JSON response."""
        ...

    def exchange(
        self,
        path: str,
        body: dict[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> TransportReply:
        """Send ``body`` to ``path`` and return the decoded JSON response with its headers."""
        ...


class HttpxTransport:
    """A real network transport to a base URL (used in the running docker stack)."""

    def __init__(self, base_url: str, *, timeout: float = 60.0) -> None:
        """Hold the base URL and per-request timeout for the target service."""
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def exchange(
        self,
        path: str,
        body: dict[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> TransportReply:
        """POST ``body`` as JSON to ``base_url + path`` and return the decoded reply with its headers."""
        resp = httpx2.post(
            f"{self._base_url}{path}",
            json=body,
            headers=dict(headers) if headers else None,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return TransportReply(result, resp.headers)

    def post_json(
        self,
        path: str,
        body: dict[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST ``body`` as JSON to ``base_url + path`` and return the decoded reply."""
        return self.exchange(path, body, headers).body
