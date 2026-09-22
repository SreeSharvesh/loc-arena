"""HTTP transport shared by the edge and the client.

Enforces nothing on its own; it is the plumbing the tamperable edge uses to relay to the sealed core and
the in-sandbox client uses to reach the edge. A ``Transport`` is a single ``post_json`` call, so tests can
inject an in-process transport (over the FastAPI app) in place of a real network client.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx


class Transport(Protocol):
    """Post a JSON body to a path and return the parsed JSON reply."""

    def post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Send ``body`` to ``path`` and return the decoded JSON response."""
        ...


class HttpxTransport:
    """A real network transport to a base URL (used in the running docker stack)."""

    def __init__(self, base_url: str, *, timeout: float = 60.0) -> None:
        """Hold the base URL and per-request timeout for the target service."""
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST ``body`` as JSON to ``base_url + path`` and return the decoded reply."""
        resp = httpx.post(f"{self._base_url}{path}", json=body, timeout=self._timeout)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result
