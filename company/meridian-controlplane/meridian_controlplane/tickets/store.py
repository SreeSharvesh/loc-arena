"""Meridian's ticket tracker: projects, their tickets and the discussion on each, in one SQLite database.

A project has a key, such as ``EVALKIT``, that never changes, and each of its tickets is ``<KEY>-<n>``,
numbered in the order it was filed. A ticket's status is open, in_progress or closed, in any order. It may
have a parent in the same project, one level deep: a parent has no parent, and a ticket with sub-tickets takes
none. Comments are append-only and nothing is ever deleted. Every creation, change and comment is also a row
of the ``history`` table, with who made it, when, and the old and new values; nothing the service serves
reads it back.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

Status = Literal["open", "in_progress", "closed"]
PROJECT_KEY_PATTERN = r"^[A-Z][A-Z0-9]{1,11}$"
SEARCH_LIMIT = 50  # the most tickets one search returns, newest change first

_SCHEMA = """
CREATE TABLE projects (
    key TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
    created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE tickets (
    id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects (key), number INTEGER NOT NULL,
    title TEXT NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL, creator TEXT NOT NULL,
    assignee TEXT, parent TEXT REFERENCES tickets (id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (project, number)
);
CREATE TABLE comments (
    id INTEGER PRIMARY KEY, ticket TEXT NOT NULL REFERENCES tickets (id), author TEXT NOT NULL,
    body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE history (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, subject TEXT NOT NULL,
    action TEXT NOT NULL, field TEXT, old_value TEXT, new_value TEXT
);
"""
_KEY_COLUMN = {"projects": "key", "tickets": "id"}


class TicketError(Exception):
    """A request the tracker refuses, with the HTTP status that says why."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        """Refuse with ``status`` and a message the caller reads."""
        super().__init__(message)
        self.status = status


class Project(BaseModel):
    """A project: its key, name and description, who created it and when it last changed."""

    model_config = ConfigDict(frozen=True)

    key: str
    name: str
    description: str
    created_by: str
    created_at: AwareDatetime
    updated_at: AwareDatetime


class TicketSummary(BaseModel):
    """A ticket as a search lists it."""

    model_config = ConfigDict(frozen=True)

    id: str
    project: str
    title: str
    status: Status
    assignee: str | None
    creator: str
    parent: str | None
    updated_at: AwareDatetime


class Comment(BaseModel):
    """One comment on a ticket."""

    model_config = ConfigDict(frozen=True)

    author: str
    body: str
    created_at: AwareDatetime


class Ticket(TicketSummary):
    """A ticket in full: its description, its sub-tickets and its comments, oldest first."""

    description: str
    created_at: AwareDatetime
    sub_tickets: list[str]
    comments: list[Comment]


class TicketStore:
    """The tracker's database. A refused change raises ``TicketError`` and changes nothing."""

    def __init__(self, database: Path, principals: Iterable[str]) -> None:
        """Open ``database``, creating its tables if it is new; ``principals`` may be assigned tickets."""
        is_new = not database.exists()
        database.parent.mkdir(parents=True, exist_ok=True)
        # Opened before the server's event loop starts, which then runs every call, one at a time.
        self._connection = sqlite3.connect(database, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        # SQLite's lower() folds ASCII alone; a search ignores case in every script.
        self._connection.create_function("casefold", 1, str.casefold, deterministic=True)
        self._connection.execute("PRAGMA foreign_keys = ON")
        if is_new:
            self._connection.executescript(_SCHEMA)
        self._principals = frozenset(principals)

    def close(self) -> None:
        """Close the database."""
        self._connection.close()

    def list_projects(self) -> list[Project]:
        """Every project, by key."""
        rows = self._connection.execute("SELECT * FROM projects ORDER BY key").fetchall()
        return [Project.model_validate(dict(row)) for row in rows]

    def create_project(
        self,
        actor: str,
        key: str,
        name: str,
        description: str,
        at: datetime | None = None,
    ) -> Project:
        """A new project; 409 if ``key`` is taken."""
        if self._find_project(key) is not None:
            raise TicketError(HTTPStatus.CONFLICT, f"project {key} already exists")
        _require_text(name, "name")
        when = _timestamp(at)
        with self._connection:
            self._connection.execute(
                "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?)",
                (key, name, description, actor, when, when),
            )
            self._record(when, actor, key, "created", None, None, {"name": name, "description": description})
        return self.get_project(key)

    def get_project(self, key: str) -> Project:
        """The project ``key``; 404 if there is none."""
        row = self._find_project(key)
        if row is None:
            raise TicketError(HTTPStatus.NOT_FOUND, f"no project {key}")
        return Project.model_validate(dict(row))

    def edit_project(
        self,
        actor: str,
        key: str,
        name: str | None,
        description: str | None,
        at: datetime | None = None,
    ) -> Project:
        """The project ``key`` with the given name or description; ``None`` keeps a field."""
        current = self.get_project(key)
        if name is not None:
            _require_text(name, "name")
        changes = _given({"name": name, "description": description})
        self._write_changes(actor, "projects", key, current.model_dump(), changes, at)
        return self.get_project(key)

    def create_ticket(  # noqa: PLR0913 - a ticket's fields, its author, its time and its number
        self,
        actor: str,
        project: str,
        title: str,
        description: str,
        assignee: str | None,
        parent: str | None,
        at: datetime | None = None,
        number: int | None = None,
    ) -> Ticket:
        """A new open ticket in ``project``, numbered ``number`` or after its last."""
        self.get_project(project)
        _require_text(title, "title")
        if assignee is not None:
            self._check_assignee(assignee)
        if parent is not None:
            self._check_parent(project, parent)
        if number is None:
            query = "SELECT COALESCE(MAX(number), 0) + 1 FROM tickets WHERE project = ?"
            number = self._connection.execute(query, (project,)).fetchone()[0]
        ticket_id, when = f"{project}-{number}", _timestamp(at)
        with self._connection:
            self._connection.execute(
                "INSERT INTO tickets VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
                (ticket_id, project, number, title, description, actor, assignee, parent, when, when),
            )
            fields = {"title": title, "description": description, "status": "open"}
            self._record(
                when,
                actor,
                ticket_id,
                "created",
                None,
                None,
                {**fields, "assignee": assignee, "parent": parent},
            )
        return self.get_ticket(ticket_id)

    def get_ticket(self, ticket_id: str) -> Ticket:
        """The ticket ``ticket_id`` in full; 404 if there is none."""
        row = self._find_ticket(ticket_id)
        if row is None:
            raise TicketError(HTTPStatus.NOT_FOUND, f"no ticket {ticket_id}")
        sub_tickets = self._connection.execute(
            "SELECT id FROM tickets WHERE parent = ? ORDER BY number",
            (ticket_id,),
        ).fetchall()
        comments = self._connection.execute(
            "SELECT author, body, created_at FROM comments WHERE ticket = ? ORDER BY id",
            (ticket_id,),
        ).fetchall()
        return Ticket.model_validate(
            {
                **dict(row),
                "sub_tickets": [sub["id"] for sub in sub_tickets],
                "comments": [dict(comment) for comment in comments],
            },
        )

    def edit_ticket(  # noqa: PLR0913 - the fields a ticket can change, its editor and its time
        self,
        actor: str,
        ticket_id: str,
        title: str | None = None,
        description: str | None = None,
        status: Status | None = None,
        assignee: str | None = None,
        parent: str | None = None,
        at: datetime | None = None,
    ) -> Ticket:
        """The ticket with the given fields: ``None`` keeps a field, ``""`` clears assignee or parent."""
        current = self.get_ticket(ticket_id)
        if title is not None:
            _require_text(title, "title")
        if assignee:
            self._check_assignee(assignee)
        if parent:
            if parent == ticket_id or current.sub_tickets:
                raise TicketError(HTTPStatus.BAD_REQUEST, f"{ticket_id} cannot be a sub-ticket of {parent}")
            self._check_parent(current.project, parent)
        fields = {"title": title, "description": description, "status": status, "assignee": assignee}
        changes = _given({**fields, "parent": parent})
        for clearable in ("assignee", "parent"):
            if changes.get(clearable) == "":
                changes[clearable] = None
        self._write_changes(actor, "tickets", ticket_id, current.model_dump(), changes, at)
        return self.get_ticket(ticket_id)

    def comment_on_ticket(self, actor: str, ticket_id: str, body: str, at: datetime | None = None) -> Comment:
        """A new comment by ``actor`` on the ticket."""
        self.get_ticket(ticket_id)
        _require_text(body, "body")
        when = _timestamp(at)
        with self._connection:
            self._connection.execute(
                "INSERT INTO comments (ticket, author, body, created_at) VALUES (?, ?, ?, ?)",
                (ticket_id, actor, body, when),
            )
            self._connection.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (when, ticket_id))
            self._record(when, actor, ticket_id, "commented", "comment", None, body)
        return Comment(author=actor, body=body, created_at=datetime.fromisoformat(when))

    def search_tickets(  # noqa: PLR0913 - every filter a search takes
        self,
        project: str | None = None,
        status: Status | None = None,
        assignee: str | None = None,
        creator: str | None = None,
        parent: str | None = None,
        updated_since: datetime | None = None,
        text: str | None = None,
    ) -> list[TicketSummary]:
        """The tickets matching every filter given, most recently changed first.

        ``text`` matches, ignoring case, any part of a title, a description or a comment.
        """
        equal = {
            "project": project,
            "status": status,
            "assignee": assignee,
            "creator": creator,
            "parent": parent,
        }
        clauses = [f"{column} = ?" for column, value in equal.items() if value is not None]
        values: list[object] = [value for value in equal.values() if value is not None]
        if updated_since is not None:
            clauses.append("updated_at > ?")
            values.append(_timestamp(updated_since))
        if text:
            clauses.append(
                "(instr(casefold(title), casefold(?)) OR instr(casefold(description), casefold(?)) OR EXISTS "
                "(SELECT 1 FROM comments WHERE ticket = tickets.id AND instr(casefold(body), casefold(?))))",
            )
            values += [text, text, text]
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM tickets {where} ORDER BY updated_at DESC, id LIMIT ?"  # noqa: S608 - values are bound
        rows = self._connection.execute(query, [*values, SEARCH_LIMIT]).fetchall()
        return [TicketSummary.model_validate(dict(row)) for row in rows]

    def _write_changes(  # noqa: PLR0913 - the row to change, its current fields, the changes and their time
        self,
        actor: str,
        table: Literal["projects", "tickets"],
        key: str,
        current: dict[str, object],
        changes: dict[str, str | None],
        at: datetime | None,
    ) -> None:
        """Write each change that differs from ``current``, one history row each; 400 if none was asked."""
        if not changes:
            raise TicketError(HTTPStatus.BAD_REQUEST, "nothing to change")
        differing = {field: value for field, value in changes.items() if value != current[field]}
        if not differing:
            return
        when = _timestamp(at)
        assignments = ", ".join(f"{field} = ?" for field in differing)
        with self._connection:
            self._connection.execute(
                f"UPDATE {table} SET {assignments}, updated_at = ? WHERE {_KEY_COLUMN[table]} = ?",  # noqa: S608 - fixed column names only
                [*differing.values(), when, key],
            )
            for field, value in differing.items():
                self._record(when, actor, key, "changed", field, current[field], value)

    def _record(  # noqa: PLR0913 - one history row's columns
        self,
        at: str,
        actor: str,
        subject: str,
        action: str,
        field: str | None,
        old: object,
        new: object,
    ) -> None:
        self._connection.execute(
            "INSERT INTO history (at, actor, subject, action, field, old_value, new_value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (at, actor, subject, action, field, _history_value(old), _history_value(new)),
        )

    def _find_project(self, key: str) -> sqlite3.Row | None:
        return self._connection.execute("SELECT * FROM projects WHERE key = ?", (key,)).fetchone()

    def _find_ticket(self, ticket_id: str) -> sqlite3.Row | None:
        return self._connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()

    def _check_assignee(self, assignee: str) -> None:
        if assignee not in self._principals:
            raise TicketError(HTTPStatus.BAD_REQUEST, f"no one named {assignee!r} can be assigned")

    def _check_parent(self, project: str, parent: str) -> None:
        row = self._find_ticket(parent)
        if row is None:
            raise TicketError(HTTPStatus.NOT_FOUND, f"no ticket {parent}")
        if row["project"] != project:
            raise TicketError(HTTPStatus.BAD_REQUEST, f"{parent} is in another project than {project}")
        if row["parent"] is not None:
            raise TicketError(HTTPStatus.BAD_REQUEST, f"{parent} is itself a sub-ticket of {row['parent']}")


def _timestamp(at: datetime | None) -> str:
    """``at``, or now, in UTC and one fixed format, so stored times sort as text."""
    return (at or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="microseconds")


def _given(fields: dict[str, str | None]) -> dict[str, str | None]:
    """The fields a caller gave a value, which it asks to change."""
    return {field: value for field, value in fields.items() if value is not None}


def _require_text(value: str, field: str) -> None:
    if not value.strip():
        raise TicketError(HTTPStatus.BAD_REQUEST, f"{field} cannot be empty")


def _history_value(value: object) -> str | None:
    """A history column's text: strings as they are, a creation's fields as JSON."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, default=str)
