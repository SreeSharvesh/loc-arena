"""Meridian's chat: channels, their threads and direct messages, in one SQLite database.

Every message has an id, given in the order it was written. A message in a channel starts a thread or replies
to one, one level deep; a direct message goes from one member to another and has no thread. Anyone can post in
any channel and message any member. Only a message's author can edit it, and the text an edit replaces is kept
in the ``edits`` table; nothing is ever deleted. A member's read position is the last message its ``unread``
covered.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict

CHANNEL_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,79}$"
READ_LIMIT = 50  # the most messages one read or search returns
MENTION = re.compile(r"@([\w.-]*\w)")  # "@mei," and "@mei." mention mei; "@batch-runner" one name

_SCHEMA = """
CREATE TABLE channels (
    name TEXT PRIMARY KEY, topic TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY, channel TEXT REFERENCES channels (name), thread INTEGER REFERENCES messages (id),
    recipient TEXT, author TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, edited_at TEXT
);
CREATE TABLE edits (
    id INTEGER PRIMARY KEY, message INTEGER NOT NULL REFERENCES messages (id), replaced TEXT NOT NULL,
    edited_at TEXT NOT NULL
);
CREATE TABLE read_positions (member TEXT PRIMARY KEY, message INTEGER NOT NULL);
"""
_SELECT = (
    "SELECT *, (SELECT COUNT(*) FROM messages AS reply WHERE reply.thread = messages.id) AS replies "
    "FROM messages"
)


class ChatError(Exception):
    """A request the chat refuses, with the HTTP status that says why."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        """Refuse with ``status`` and a message the caller reads."""
        super().__init__(message)
        self.status = status


class Channel(BaseModel):
    """A channel: its name and topic, and who created it when."""

    model_config = ConfigDict(frozen=True)

    name: str
    topic: str
    created_by: str
    created_at: AwareDatetime


class Message(BaseModel):
    """One message: in a channel, starting a thread or replying to one, or sent directly to a member."""

    model_config = ConfigDict(frozen=True)

    id: int
    channel: str | None
    thread: int | None
    recipient: str | None
    author: str
    body: str
    created_at: AwareDatetime
    edited_at: AwareDatetime | None
    replies: int


class ChatStore:
    """The chat's database. A refused change raises ``ChatError`` and changes nothing."""

    def __init__(self, database: Path, members: Iterable[str]) -> None:
        """Open ``database``, creating its tables if it is new; ``members`` can be messaged and mentioned."""
        is_new = not database.exists()
        database.parent.mkdir(parents=True, exist_ok=True)
        # Opened before the server's event loop starts, which then runs every call, one at a time.
        self._connection = sqlite3.connect(database, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        # SQLite's lower() folds ASCII alone; a search ignores case in every script.
        self._connection.create_function("casefold", 1, str.casefold, deterministic=True)
        self._connection.create_function("mentions", 2, _mentions, deterministic=True)
        self._connection.execute("PRAGMA foreign_keys = ON")
        if is_new:
            self._connection.executescript(_SCHEMA)
        self._members = frozenset(members)

    def close(self) -> None:
        """Close the database."""
        self._connection.close()

    def list_channels(self) -> list[Channel]:
        """Every channel, by name."""
        rows = self._connection.execute("SELECT * FROM channels ORDER BY name").fetchall()
        return [Channel.model_validate(dict(row)) for row in rows]

    def create_channel(self, actor: str, name: str, topic: str, at: datetime | None = None) -> Channel:
        """A new channel; 409 if ``name`` is taken."""
        if self._find_channel(name) is not None:
            raise ChatError(HTTPStatus.CONFLICT, f"channel #{name} already exists")
        when = _timestamp(at)
        with self._connection:
            self._connection.execute("INSERT INTO channels VALUES (?, ?, ?, ?)", (name, topic, actor, when))
        return Channel(name=name, topic=topic, created_by=actor, created_at=datetime.fromisoformat(when))

    def read_channel(
        self,
        channel: str,
        thread: int | None = None,
        before: int | None = None,
    ) -> list[Message]:
        """The latest threads of ``channel`` before message ``before``, or the messages of thread ``thread``.

        Either way the messages come oldest first, at most ``READ_LIMIT`` of them.
        """
        self._require_channel(channel)
        if thread is None:
            return self._latest("channel = ? AND thread IS NULL", [channel], before)
        if self._find_message(thread, "channel = ? AND thread IS NULL", channel) is None:
            raise ChatError(HTTPStatus.NOT_FOUND, f"no thread {thread} in #{channel}")
        return self._latest("id = ? OR thread = ?", [thread, thread], before)

    def post(self, actor: str, channel: str, body: str, at: datetime | None = None) -> Message:
        """A new thread by ``actor`` in ``channel``."""
        self._require_channel(channel)
        return self._insert(actor, body, at, channel=channel)

    def reply(self, actor: str, message: int, body: str, at: datetime | None = None) -> Message:
        """A reply by ``actor`` in the thread that ``message`` starts; 400 unless it starts one."""
        row = self._find_message(message)
        if row is None:
            raise ChatError(HTTPStatus.NOT_FOUND, f"no message {message}")
        if row["channel"] is None or row["thread"] is not None:
            raise ChatError(HTTPStatus.BAD_REQUEST, f"message {message} does not start a thread in a channel")
        return self._insert(actor, body, at, channel=row["channel"], thread=message)

    def send_dm(self, actor: str, member: str, body: str, at: datetime | None = None) -> Message:
        """A direct message from ``actor`` to ``member``; 400 if the chat has no such member."""
        if member not in self._members:
            raise ChatError(HTTPStatus.BAD_REQUEST, f"no one named {member!r} is in the chat")
        return self._insert(actor, body, at, recipient=member)

    def read_dms(self, actor: str, member: str | None = None, before: int | None = None) -> list[Message]:
        """The latest direct messages ``actor`` sent or received, with ``member`` if given, oldest first."""
        clause = "recipient IS NOT NULL AND (author = ? OR recipient = ?)"
        values: list[object] = [actor, actor]
        if member is not None:
            clause += " AND (author = ? OR recipient = ?)"
            values += [member, member]
        return self._latest(clause, values, before)

    def unread(self, actor: str) -> list[Message]:
        """The direct messages to ``actor`` and the channel messages mentioning it since its last ``unread``.

        They come oldest first, without its own. Its read position moves past every message written so far.
        """
        row = self._connection.execute(
            "SELECT message FROM read_positions WHERE member = ?",
            (actor,),
        ).fetchone()
        with self._connection:
            rows = self._connection.execute(
                f"{_SELECT} WHERE id > ? AND author != ? AND (recipient = ? OR "  # noqa: S608 - values are bound
                "(channel IS NOT NULL AND mentions(body, ?))) ORDER BY created_at, id",
                (row["message"] if row else 0, actor, actor, actor),
            ).fetchall()
            self._connection.execute(
                "INSERT INTO read_positions VALUES (?, (SELECT COALESCE(MAX(id), 0) FROM messages)) "
                "ON CONFLICT (member) DO UPDATE SET message = excluded.message",
                (actor,),
            )
        return [Message.model_validate(dict(row)) for row in rows]

    def search(
        self,
        actor: str,
        text: str,
        channel: str | None = None,
        author: str | None = None,
    ) -> list[Message]:
        """The messages containing ``text``, ignoring case, in channels or ``actor``'s DMs, newest first."""
        clauses = [
            "instr(casefold(body), casefold(?))",
            "(channel IS NOT NULL OR author = ? OR recipient = ?)",
        ]
        values: list[object] = [text, actor, actor]
        for column, value in (("channel", channel), ("author", author)):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        query = f"{_SELECT} WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, id DESC LIMIT ?"  # noqa: S608 - values are bound
        rows = self._connection.execute(query, [*values, READ_LIMIT]).fetchall()
        return [Message.model_validate(dict(row)) for row in rows]

    def edit_message(self, actor: str, message: int, body: str, at: datetime | None = None) -> Message:
        """The message with ``body`` as its text; 403 unless ``actor`` wrote it."""
        row = self._find_message(message)
        if row is None:
            raise ChatError(HTTPStatus.NOT_FOUND, f"no message {message}")
        if row["author"] != actor:
            raise ChatError(
                HTTPStatus.FORBIDDEN,
                f"only its author, {row['author']}, can edit message {message}",
            )
        _require_text(body)
        if body != row["body"]:
            when = _timestamp(at)
            with self._connection:
                self._connection.execute(
                    "INSERT INTO edits (message, replaced, edited_at) VALUES (?, ?, ?)",
                    (message, row["body"], when),
                )
                self._connection.execute(
                    "UPDATE messages SET body = ?, edited_at = ? WHERE id = ?",
                    (body, when, message),
                )
        return self._get_message(message)

    def _insert(  # noqa: PLR0913 - a message's author, text and time, and where it goes
        self,
        actor: str,
        body: str,
        at: datetime | None,
        channel: str | None = None,
        thread: int | None = None,
        recipient: str | None = None,
    ) -> Message:
        _require_text(body)
        with self._connection:
            cursor = self._connection.execute(
                "INSERT INTO messages (channel, thread, recipient, author, body, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (channel, thread, recipient, actor, body, _timestamp(at)),
            )
        return self._get_message(cursor.lastrowid or 0)

    def _latest(self, clause: str, values: list[object], before: int | None) -> list[Message]:
        """The latest ``READ_LIMIT`` messages matching ``clause``, before message ``before``, oldest first."""
        if before is not None:
            clause = f"({clause}) AND (created_at, id) < (SELECT created_at, id FROM messages WHERE id = ?)"
            values = [*values, before]
        query = f"{_SELECT} WHERE {clause} ORDER BY created_at DESC, id DESC LIMIT ?"  # noqa: S608 - fixed clauses only
        rows = self._connection.execute(query, [*values, READ_LIMIT]).fetchall()
        return [Message.model_validate(dict(row)) for row in reversed(rows)]

    def _get_message(self, message: int) -> Message:
        return Message.model_validate(
            dict(self._connection.execute(f"{_SELECT} WHERE id = ?", (message,)).fetchone()),
        )

    def _find_message(self, message: int, clause: str = "1", *values: object) -> sqlite3.Row | None:
        query = f"SELECT * FROM messages WHERE id = ? AND {clause}"  # noqa: S608 - fixed clauses only
        return self._connection.execute(query, (message, *values)).fetchone()

    def _find_channel(self, name: str) -> sqlite3.Row | None:
        return self._connection.execute("SELECT * FROM channels WHERE name = ?", (name,)).fetchone()

    def _require_channel(self, name: str) -> None:
        if self._find_channel(name) is None:
            raise ChatError(HTTPStatus.NOT_FOUND, f"no channel #{name}")


def _timestamp(at: datetime | None) -> str:
    """``at``, or now, in UTC and one fixed format, so stored times sort as text."""
    return (at or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="microseconds")


def _require_text(body: str) -> None:
    if not body.strip():
        raise ChatError(HTTPStatus.BAD_REQUEST, "a message cannot be empty")


def _mentions(body: str, member: str) -> bool:
    """Whether ``body`` mentions ``member`` as ``@member``."""
    return member in MENTION.findall(body)
