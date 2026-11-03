"""What the forge knows beyond its repositories, in a SQLite file beside them, so a restart keeps it.

The repositories hold the code; this holds the pull requests open and merged on it, who pushed each commit the
forge took, and whether the forge was set up: the seed's pull requests opened and the deployment's first
changes made. A forge that starts on a store it set up before reads them back and sets up nothing again, so a
restart keeps every pull request and its number.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path

from pydantic import TypeAdapter

from meridian_forge.models import PullRequest

STORE_FILE = "forge.db"  # in the directory of the forge's repositories
_SCHEMA = """
CREATE TABLE IF NOT EXISTS pull_requests (repo TEXT, number INTEGER, fields TEXT, PRIMARY KEY (repo, number));
CREATE TABLE IF NOT EXISTS pushes (repo TEXT, commit_id TEXT, pusher TEXT, PRIMARY KEY (repo, commit_id));
CREATE TABLE IF NOT EXISTS facts (name TEXT PRIMARY KEY);
"""
_SET_UP = "set up"  # the fact that the forge's set-up is done
_PULL_REQUEST = TypeAdapter(PullRequest)


class ForgeStore:
    """The forge's pull requests, its pushes' authors and its set-up, in the SQLite file ``database``."""

    def __init__(self, database: Path) -> None:
        """Open ``database``, creating its tables if it is new."""
        database.parent.mkdir(parents=True, exist_ok=True)
        # Opened before the server's event loop starts, which then runs every call, one at a time.
        self._connection = sqlite3.connect(database, isolation_level=None, check_same_thread=False)
        self._connection.executescript(_SCHEMA)

    def is_set_up(self) -> bool:
        """Whether a forge on this store was set up before: then it is a restart, which sets up nothing."""
        found = self._connection.execute("SELECT 1 FROM facts WHERE name = ?", (_SET_UP,)).fetchone()
        return found is not None

    def mark_set_up(self) -> None:
        """Note that the forge on this store is set up."""
        self._connection.execute("INSERT OR IGNORE INTO facts VALUES (?)", (_SET_UP,))

    def pull_requests(self) -> list[PullRequest]:
        """Every pull request kept, by repo and number."""
        rows = self._connection.execute("SELECT fields FROM pull_requests ORDER BY repo, number").fetchall()
        return [_PULL_REQUEST.validate_json(fields) for (fields,) in rows]

    def save_pull_requests(self, pull_requests: Iterable[PullRequest]) -> None:
        """Keep each of ``pull_requests`` as it is now, in place of what was kept of it."""
        rows = [(pr.repo, pr.number, _PULL_REQUEST.dump_json(pr)) for pr in pull_requests]
        self._connection.executemany("INSERT OR REPLACE INTO pull_requests VALUES (?, ?, ?)", rows)

    def pushers(self, repo: str) -> dict[str, str | None]:
        """Who pushed each commit a push brought to ``repo``, by commit."""
        rows = self._connection.execute("SELECT commit_id, pusher FROM pushes WHERE repo = ?", (repo,))
        return dict(rows.fetchall())

    def save_pushes(self, repo: str, pushed: Mapping[str, str | None]) -> None:
        """Keep who pushed each commit of ``pushed`` to ``repo``, by commit."""
        rows = [(repo, commit, pusher) for commit, pusher in pushed.items()]
        self._connection.executemany("INSERT OR REPLACE INTO pushes VALUES (?, ?, ?)", rows)
