"""How the edge reaches the core.

Enforces nothing on its own. A ``Transport`` is a single ``post_json`` call: a run hands the edge the core in
its own process (``core.DirectTransport``), and tests inject one over the core's FastAPI app.
"""

from __future__ import annotations

from typing import Any, Protocol


class Transport(Protocol):
    """Post a JSON body to a path and return the parsed JSON reply."""

    def post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Send ``body`` to ``path`` and return the decoded JSON response."""
        ...
