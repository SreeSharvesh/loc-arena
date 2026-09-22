"""The ``Services`` adapter that backs the agent's forge tools (routed inference; fingerprinted actions).

Wires the scaffold tool catalog's git/PR/ticket/wiki tools to the in-process ``Forge``. It implements the
``Services`` protocol the ``Tools`` layer calls, so every forge tool an agent runs is scope-enforced and
recorded as a fingerprinted action event by the ``Tools`` layer (this adapter never records; it only
dispatches). Tools this adapter does not own fall through to a canned stub result, exactly as before, so the
rest of the service surface is unchanged.
"""

from __future__ import annotations

from typing import Any

from loc_arena.forge.forge import Forge, ForgeError

# Forge-backed tools -> Forge method name.
_FORGE_TOOLS: dict[str, str] = {
    "git": "git",
    "open_pr": "open_pr",
    "pr_status": "pr_status",
    "merge": "merge",
    "read_ticket": "read_ticket",
    "list_tickets": "list_tickets",
    "update_ticket": "update_ticket",
    "read_wiki": "read_wiki",
    "search_wiki": "search_wiki",
}


class ForgeServices:
    """A ``Services`` implementation that routes forge tools to a real ``Forge`` and stubs the rest."""

    def __init__(self, forge: Forge) -> None:
        """Back the forge-owned tools with ``forge``; other tools return a canned stub result."""
        self._forge = forge

    def run(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a forge tool to the ``Forge`` (a bad reference is a structured error, not a crash)."""
        method = _FORGE_TOOLS.get(tool)
        if method is None:
            return {"stub": True, "tool": tool}
        try:
            result: dict[str, Any] = getattr(self._forge, method)(args)
        except ForgeError as exc:
            return {"error": str(exc), "tool": tool}
        return result
