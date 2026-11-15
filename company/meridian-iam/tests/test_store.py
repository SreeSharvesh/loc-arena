"""The store's transaction: a commit that fails must not leave the connection stuck in a transaction."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from meridian_iam.core.store import _MIGRATIONS, SCHEMA_VERSION, connect, transaction


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
    old.execute("DROP TABLE contracts")
    old.execute("DROP TABLE stagers")
    old.execute("PRAGMA user_version = 1")  # what a store from before workloads looks like
    old.close()

    upgraded = connect(path)

    tables = {row[0] for row in upgraded.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"workloads", "delegations", "stagers", "contracts"} <= tables
    assert upgraded.execute("SELECT value FROM meta WHERE key = 'kept'").fetchone()[0] == "yes"


def test_an_upgraded_store_keeps_each_resources_operations_as_its_base_contract(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = connect(path)
    old.execute(
        "INSERT INTO resources VALUES ('notes', 'research', 'lead', 3600, 0, 20, '{\"read_note\": {}}')",
    )
    for table in ("contracts", "stagers", "workloads", "delegations"):
        old.execute(f"DROP TABLE {table}")
    old.execute("PRAGMA user_version = 1")
    old.close()

    upgraded = connect(path)

    base = upgraded.execute("SELECT deployment, operations FROM contracts WHERE state = 'base'").fetchall()
    assert [tuple(row) for row in base] == [("image", '{"read_note": {}}')]


def test_two_openers_of_an_old_store_upgrade_it_once(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = connect(path)
    old.execute("DROP TABLE workloads")
    old.execute("DROP TABLE delegations")
    old.execute("DROP TABLE contracts")
    old.execute("DROP TABLE stagers")
    old.execute("PRAGMA user_version = 1")
    old.close()
    first = sqlite3.connect(path, isolation_level=None)
    first.execute("BEGIN IMMEDIATE")  # the first opener holds the write lock while it upgrades
    opened: list[sqlite3.Connection] = []
    second = threading.Thread(target=lambda: opened.append(connect(path, timeout=5)))
    second.start()
    time.sleep(0.3)  # the second opener has read version 1 and waits for the lock
    for script in _MIGRATIONS[1:]:
        for statement in script.split(";"):
            if statement.strip():
                first.execute(statement)
    first.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    first.execute("COMMIT")

    second.join(timeout=10)

    assert opened, "the second opener failed instead of finding the store already upgraded"
    assert opened[0].execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_a_failed_upgrade_leaves_the_store_as_it_was_and_unlocked(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = connect(path)
    old.execute("DROP TABLE workloads")
    old.execute("DROP TABLE delegations")
    old.execute("DROP TABLE contracts")
    old.execute("DROP TABLE stagers")
    old.execute("CREATE TABLE workloads (squatter TEXT)")  # the upgrade's second table cannot be created
    old.execute("PRAGMA user_version = 1")
    old.close()

    with pytest.raises(sqlite3.OperationalError, match="already exists") as failed:
        connect(path)

    raw = sqlite3.connect(path, isolation_level=None, timeout=0.1)
    tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "delegations" not in tables  # the upgrade's first table was rolled back
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
    raw.execute("BEGIN IMMEDIATE")  # nothing holds the write lock, though the error is still referenced
    raw.execute("ROLLBACK")
    assert failed.value
