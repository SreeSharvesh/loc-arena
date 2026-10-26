"""The team's shared notes: plain text under a short key, held in memory.

A key is 1 to 64 letters, digits, ``.``, ``_`` or ``-``. The store holds at most ``MAX_NOTES`` notes of at
most ``MAX_NOTE_BYTES`` bytes of UTF-8 each; a write past either bound is refused and changes nothing.
"""

from __future__ import annotations

from http import HTTPStatus

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
    """The notes by key. A refused change raises ``NoteError``."""

    def __init__(self) -> None:
        """An empty store."""
        self._notes: dict[str, str] = {}

    def list_notes(self) -> list[str]:
        """Every note's key, in order."""
        return sorted(self._notes)

    def read_note(self, key: str) -> str:
        """The text of note ``key``; 404 if there is none."""
        if key not in self._notes:
            raise NoteError(HTTPStatus.NOT_FOUND, f"no note {key}")
        return self._notes[key]

    def write_note(self, key: str, text: str) -> bool:
        """Note ``key`` now holds ``text``; whether it is new. 413 if too large, 507 if the store is full."""
        if len(text.encode()) > MAX_NOTE_BYTES:
            raise NoteError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"a note holds at most {MAX_NOTE_BYTES} bytes",
            )
        created = key not in self._notes
        if created and len(self._notes) >= MAX_NOTES:
            raise NoteError(HTTPStatus.INSUFFICIENT_STORAGE, f"there are already {MAX_NOTES} notes")
        self._notes[key] = text
        return created

    def delete_note(self, key: str) -> None:
        """Note ``key`` is gone; 404 if there is none."""
        self.read_note(key)
        del self._notes[key]
