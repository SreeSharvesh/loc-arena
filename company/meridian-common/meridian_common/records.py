"""Records that outlive their process: mappings kept in one SQLite database.

A service opens one ``Database`` and keeps each kind of record in a ``Records`` of it: a mapping from a key
to a value, stored as the JSON text the service's own ``dump`` gives and read back with its ``load``. Every
read comes from the database, so a process that starts again on the same file holds what the last one wrote.
A write is kept at once. Writes that belong together go in one ``transaction``: all of them are kept, or
none, also when the process is killed halfway.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Callable, Iterator, MutableMapping
from pathlib import Path


class Database:
    """One SQLite file of records, shared by the threads of one process."""

    def __init__(self, path: Path | None = None) -> None:
        """Open the database at ``path``, new if there is none; in memory, for one process, without one."""
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite's autocommit mode: each statement is kept at once, and a transaction is opened by name.
        self._connection = sqlite3.connect(path or ":memory:", autocommit=True, check_same_thread=False)
        self._lock = threading.RLock()  # one thread at a time: sqlite3 leaves serializing writes to its user
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS records (collection TEXT NOT NULL, key TEXT NOT NULL, "
            "body TEXT NOT NULL, PRIMARY KEY (collection, key))",
        )

    def records[T](self, collection: str, load: Callable[[str], T], dump: Callable[[T], str]) -> Records[T]:
        """The records of ``collection``: ``dump`` gives a value's JSON text, ``load`` reads it back."""
        return Records(self, collection, load, dump)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """Keep every change made inside, or none of them if it raises. Not to be opened inside another."""
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                yield
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def execute(self, statement: str, *parameters: str) -> list[tuple[str, ...]]:
        """Run ``statement`` with ``parameters`` and return its rows."""
        with self._lock:
            return self._connection.execute(statement, parameters).fetchall()


class Records[T](MutableMapping[str, T]):
    """One collection of a database as a mapping, in the order its keys were first written."""

    def __init__(
        self,
        database: Database,
        collection: str,
        load: Callable[[str], T],
        dump: Callable[[T], str],
    ) -> None:
        """The records of ``collection`` in ``database``, each stored as ``dump`` gives it."""
        self._database, self._collection, self._load, self._dump = database, collection, load, dump

    def __getitem__(self, key: str) -> T:
        """The value under ``key``, as stored now."""
        found = self._database.execute(
            "SELECT body FROM records WHERE collection = ? AND key = ?",
            self._collection,
            key,
        )
        if not found:
            raise KeyError(key)
        return self._load(found[0][0])

    def __setitem__(self, key: str, value: T) -> None:
        """Store ``value`` under ``key``; a key written before keeps its place in the order."""
        self._database.execute(
            "INSERT INTO records VALUES (?, ?, ?) ON CONFLICT DO UPDATE SET body = excluded.body",
            self._collection,
            key,
            self._dump(value),
        )

    def __delitem__(self, key: str) -> None:
        """Remove ``key``; raises ``KeyError`` if there is none."""
        deleted = self._database.execute(
            "DELETE FROM records WHERE collection = ? AND key = ? RETURNING key",
            self._collection,
            key,
        )
        if not deleted:
            raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        """Every key, in the order each was first written."""
        keys = self._database.execute(
            "SELECT key FROM records WHERE collection = ? ORDER BY rowid",
            self._collection,
        )
        return iter([key for (key,) in keys])

    def __len__(self) -> int:
        """How many records the collection holds."""
        counted = self._database.execute(
            "SELECT COUNT(*) FROM records WHERE collection = ?",
            self._collection,
        )
        return int(counted[0][0])
