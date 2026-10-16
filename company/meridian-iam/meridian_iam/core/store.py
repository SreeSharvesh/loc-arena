"""SQLite schema, connection and transaction helper for the single authoritative IAM store."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_V1 = """
CREATE TABLE principals (id TEXT PRIMARY KEY, kind TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE memberships (
  principal TEXT NOT NULL REFERENCES principals(id), team TEXT NOT NULL, role TEXT NOT NULL,
  authority TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, provenance TEXT NOT NULL,
  PRIMARY KEY (principal, team));
CREATE TABLE resources (
  id TEXT PRIMARY KEY, owner_team TEXT NOT NULL, approver_role TEXT NOT NULL,
  max_seconds INTEGER NOT NULL, allow_ongoing INTEGER NOT NULL, max_pending INTEGER NOT NULL,
  operations TEXT NOT NULL);
CREATE TABLE service_bindings (
  principal TEXT PRIMARY KEY REFERENCES principals(id), resource TEXT NOT NULL REFERENCES resources(id),
  audience TEXT NOT NULL);
CREATE TABLE credentials (
  digest TEXT PRIMARY KEY, principal TEXT NOT NULL REFERENCES principals(id), audiences TEXT NOT NULL,
  label TEXT NOT NULL, generation INTEGER NOT NULL, issued_at INTEGER NOT NULL, expires_at INTEGER,
  revoked_at INTEGER, superseded_by TEXT);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE requests (
  id TEXT PRIMARY KEY, requester TEXT NOT NULL REFERENCES principals(id),
  resource TEXT NOT NULL REFERENCES resources(id), permissions TEXT NOT NULL, until INTEGER,
  reason TEXT NOT NULL, state TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE decisions (
  request TEXT PRIMARY KEY REFERENCES requests(id), approver TEXT NOT NULL, approved INTEGER NOT NULL,
  permissions TEXT NOT NULL, until INTEGER, reason TEXT NOT NULL, decided_at INTEGER NOT NULL);
CREATE TABLE grants (
  id INTEGER PRIMARY KEY AUTOINCREMENT, principal TEXT NOT NULL REFERENCES principals(id),
  resource TEXT NOT NULL REFERENCES resources(id), permission TEXT NOT NULL, starts_at INTEGER NOT NULL,
  expires_at INTEGER, source TEXT NOT NULL, request TEXT, granted_by TEXT NOT NULL, revoked_at INTEGER);
CREATE TABLE mutation_keys (
  actor TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL, result TEXT NOT NULL,
  PRIMARY KEY (actor, key));
CREATE TABLE audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, kind TEXT NOT NULL, actor TEXT,
  detail TEXT NOT NULL);
"""
# Version 2: what a service may delegate, and the workloads it issued. ``grant_horizon`` is the newest grant
# ID when the workload was issued: a sponsor grant above it came later and cannot keep the workload alive.
_V2 = """
CREATE TABLE delegations (
  issuer TEXT NOT NULL REFERENCES resources(id), resource TEXT NOT NULL REFERENCES resources(id),
  permission TEXT NOT NULL, PRIMARY KEY (issuer, resource, permission));
CREATE TABLE workloads (
  principal TEXT PRIMARY KEY REFERENCES principals(id), resource TEXT NOT NULL REFERENCES resources(id),
  issuer TEXT NOT NULL REFERENCES principals(id), sponsor TEXT NOT NULL REFERENCES principals(id),
  operation TEXT NOT NULL, reference TEXT NOT NULL, purpose TEXT NOT NULL, issued_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL, grant_horizon INTEGER NOT NULL);
"""
_MIGRATIONS = (_V1, _V2)
SCHEMA_VERSION = len(_MIGRATIONS)


def connect(path: Path | str, timeout: float = 5.0) -> sqlite3.Connection:
    """Open the store, creating or upgrading it in place; foreign keys on, explicit transactions only."""
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"unsupported IAM store version {version}")
    if version < SCHEMA_VERSION:  # a new store, or one a restarted container kept from an older release
        conn.execute("BEGIN IMMEDIATE")
        for script in _MIGRATIONS[version:]:
            for statement in script.split(";"):
                if statement.strip():
                    conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One serialized write transaction: commit on success, roll everything back on any failure.

    The commit is inside the guard too: a commit that fails (the database busy, the disk full) must not leave
    this connection stuck in an open transaction, where every later ``BEGIN`` would fail. SQLite can also
    end a transaction itself, so the rollback runs only if one is still open and never replaces the
    original error.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
