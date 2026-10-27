"""The notes service: the team's shared notes, plain text under a short key.

``store`` holds them, ``seed`` what a store starts with, and ``service`` the HTTP API and the MCP tools over a
store, authorized by the central authority a deployment hands in (``meridian_controlplane.agents.authority``).
"""

from __future__ import annotations

from meridian_controlplane.notes.seed import NoteSeed, load_note_seed, open_seeded_store
from meridian_controlplane.notes.service import SERVICE, build_notes_server, create_notes_api
from meridian_controlplane.notes.store import NoteError, NoteStore

__all__ = [
    "SERVICE",
    "NoteError",
    "NoteSeed",
    "NoteStore",
    "build_notes_server",
    "create_notes_api",
    "load_note_seed",
    "open_seeded_store",
]
