"""Records in a database: a mapping whose entries outlive the process that wrote them."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from meridian_common.records import Database, Records


def open_counts(database: Database) -> Records[int]:
    return database.records("counts", json.loads, json.dumps)


def test_a_record_written_by_one_process_is_read_by_the_next(tmp_path: Path) -> None:
    open_counts(Database(tmp_path / "service.db"))["jobs"] = 3

    reopened = open_counts(Database(tmp_path / "service.db"))

    assert dict(reopened) == {"jobs": 3}


def test_records_are_listed_in_the_order_they_were_first_written(tmp_path: Path) -> None:
    counts = open_counts(Database(tmp_path / "service.db"))
    counts["b"], counts["a"], counts["c"] = 1, 2, 3

    counts["b"] = 4

    assert list(counts.items()) == [("b", 4), ("a", 2), ("c", 3)]


def test_a_deleted_record_is_gone_and_deleting_it_again_is_a_key_error() -> None:
    counts = open_counts(Database())
    counts["jobs"] = 3

    del counts["jobs"]

    assert "jobs" not in counts
    with pytest.raises(KeyError):
        del counts["jobs"]


def test_two_collections_of_one_database_keep_their_own_keys() -> None:
    database = Database()
    counts = open_counts(database)
    names = database.records("names", json.loads, json.dumps)

    counts["first"], names["first"] = 1, "one"

    assert (counts["first"], names["first"], len(counts)) == (1, "one", 1)


def test_a_transaction_that_fails_keeps_none_of_its_changes(tmp_path: Path) -> None:
    database = Database(tmp_path / "service.db")
    counts = open_counts(database)
    counts["kept"] = 1

    with pytest.raises(RuntimeError), database.transaction():
        counts["kept"] = 2
        counts["new"] = 3
        raise RuntimeError

    assert dict(open_counts(Database(tmp_path / "service.db"))) == {"kept": 1}


def test_a_write_from_another_thread_waits_for_the_transaction_under_way() -> None:
    database = Database()
    counts = open_counts(database)
    writer = threading.Thread(target=counts.__setitem__, args=("other", 1))

    with pytest.raises(RuntimeError), database.transaction():
        writer.start()
        writer.join(0.2)  # it waits: its write is no part of this transaction
        raise RuntimeError
    writer.join()

    assert dict(counts) == {"other": 1}


def test_an_error_after_sqlite_ended_the_transaction_itself_reaches_the_caller() -> None:
    database = Database()
    counts = open_counts(database)

    with pytest.raises(RuntimeError, match="the cause"), database.transaction():
        counts["lost"] = 1
        database.execute("ROLLBACK")  # as SQLite does itself on some errors, a full disk among them
        raise RuntimeError("the cause")

    assert dict(counts) == {}


def test_what_follows_a_change_runs_once_its_transaction_is_kept_and_never_if_it_is_not() -> None:
    database = Database()
    counts = open_counts(database)
    said: list[str] = []

    database.after_commit(lambda: said.append("outside any transaction"))
    with database.transaction():
        counts["kept"] = 1
        database.after_commit(lambda: said.append(f"kept {dict(counts)}"))
        said.append("before the commit")
    with pytest.raises(RuntimeError), database.transaction():
        database.after_commit(lambda: said.append("undone"))
        raise RuntimeError
    with database.transaction():  # a later one that is kept runs nothing of the one undone
        counts["later"] = 2

    assert said == ["outside any transaction", "before the commit", "kept {'kept': 1}"]
