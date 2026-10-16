"""The store's transaction: a commit that fails must not leave the connection stuck in a transaction."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from meridian_iam.core.store import connect, transaction


def test_a_commit_that_cannot_complete_leaves_the_connection_usable(tmp_path: Path) -> None:
    path = tmp_path / "busy.db"
    ours = connect(path, timeout=0.05)
    with transaction(ours) as db:
        db.execute("INSERT INTO meta VALUES ('k', 'before')")
    reader = sqlite3.connect(path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute(
        "SELECT * FROM meta",
    ).fetchall()  # holds a shared lock, so our commit cannot get the write lock
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        with transaction(ours) as db:
            db.execute("UPDATE meta SET value = 'during' WHERE key = 'k'")
    assert not ours.in_transaction  # rolled back, not stuck open
    reader.execute("ROLLBACK")
    with transaction(ours) as db:  # and the next transaction starts instead of failing to begin inside one
        db.execute("UPDATE meta SET value = 'after' WHERE key = 'k'")
    assert ours.execute("SELECT value FROM meta WHERE key = 'k'").fetchone()[0] == "after"


def test_a_rollback_after_sqlite_ended_the_transaction_does_not_hide_the_real_error(tmp_path: Path) -> None:
    store = connect(tmp_path / "ended.db")

    class BoomError(Exception):
        pass

    with pytest.raises(BoomError):
        with transaction(store):
            store.execute("COMMIT")  # the transaction is already over, as when SQLite ends one itself
            raise BoomError
    assert not store.in_transaction


def test_a_version_1_store_is_upgraded_in_place(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = connect(path)
    old.execute("INSERT INTO meta VALUES ('kept', 'yes')")
    old.execute("DROP TABLE workloads")
    old.execute("DROP TABLE delegations")
    old.execute("PRAGMA user_version = 1")  # what a store from before workloads looks like
    old.close()

    upgraded = connect(path)

    tables = {row[0] for row in upgraded.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"workloads", "delegations"} <= tables
    assert upgraded.execute("SELECT value FROM meta WHERE key = 'kept'").fetchone()[0] == "yes"
