"""The team's shared notes: plain text under a short key, held in a database.

A key is 1 to 64 letters, digits, ``.``, ``_`` or ``-``. The store holds at most ``MAX_NOTES`` notes of at
most ``MAX_NOTE_BYTES`` bytes of UTF-8 each; a write past either bound is refused and changes nothing.
"""

from __future__ import annotations

import sqlite3
from http import HTTPStatus
from pathlib import Path

KEY_PATTERN = r"^[A-Za-z0-9._-]{1,64}$"
MAX_NOTES = 100
MAX_NOTE_BYTES = 65_536


class NoteError(Exception):
    """A request the notes refuse, with the HTTP status that says why."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        """Refuse with ``status`` and a message the caller reads."""
        super().__init__(message)
        self.status = status


class NoteStore:
    """The notes by key, in a database. A refused change raises ``NoteError`` and changes nothing."""

    def __init__(self, database: Path) -> None:
        """Open ``database``, creating its table if it is new."""
        database.parent.mkdir(parents=True, exist_ok=True)
        # Opened before the server's event loop starts, which then runs every call, one at a time.
        self._connection = sqlite3.connect(database, isolation_level=None, check_same_thread=False)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS notes (key TEXT PRIMARY KEY, text TEXT NOT NULL)",
        )

    def close(self) -> None:
        """Close the database."""
        self._connection.close()

    def list_notes(self) -> list[str]:
        """Every note's key, in order."""
        return [key for (key,) in self._connection.execute("SELECT key FROM notes ORDER BY key")]

    def read_note(self, key: str) -> str:
        """The text of note ``key``; 404 if there is none."""
        found = self._connection.execute("SELECT text FROM notes WHERE key = ?", (key,)).fetchone()
        if found is None:
            raise NoteError(HTTPStatus.NOT_FOUND, f"no note {key}")
        return found[0]

    def write_note(self, key: str, text: str) -> bool:
        """Note ``key`` now holds ``text``; whether it is new. 413 if too large, 507 if the store is full."""
        if len(text.encode()) > MAX_NOTE_BYTES:
            raise NoteError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"a note holds at most {MAX_NOTE_BYTES} bytes",
            )
        held = {key for (key,) in self._connection.execute("SELECT key FROM notes")}
        created = key not in held
        if created and len(held) >= MAX_NOTES:
            raise NoteError(HTTPStatus.INSUFFICIENT_STORAGE, f"there are already {MAX_NOTES} notes")
        self._connection.execute("INSERT OR REPLACE INTO notes (key, text) VALUES (?, ?)", (key, text))
        return created

    def delete_note(self, key: str) -> None:
        """Note ``key`` is gone; 404 if there is none."""
        self.read_note(key)
        self._connection.execute("DELETE FROM notes WHERE key = ?", (key,))
