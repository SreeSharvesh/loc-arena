"""What the tickets service needs of a central authorization service, without importing one.

The company package stays free of any platform dependency: a deployment hands in an implementation of this
protocol (under central IAM, the engine's ``loc_arena.agents_iam``). The service keeps no identity and no
rights of its own: the authority decides every call before its handler runs, and names its principal.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from fastapi import FastAPI, Request


class Authority(Protocol):
    """A central authority deciding every call of the service. Refusals are raised as ``HTTPException``."""

    dependency: Callable[[Request], Awaitable[None]]
    """The app-level dependency: it runs after routing and before any handler, setting ``request.state``
    ``iam_principal`` (the caller) and ``iam_operation`` (the operation the route is declared as)."""

    def verify_routes(self, app: FastAPI) -> None:
        """Refuse to start unless the routes of ``app`` and the declared operations agree exactly."""
        ...
