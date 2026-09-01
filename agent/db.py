"""Shared DB-connection factory for every SQLite-backed store in this
project (the ledger, event dedup, outbound-action claims, the recovery
ledger, the reversal ledger, the extractor-drift log). Local SQLite file
by default; a remote Turso (libsql) database instead when both
TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are set -- one env-var switch,
no call-site changes, since every store already just does
`self._conn = connect(self.db_path)` in place of `sqlite3.connect(...)`.

Two real incompatibilities were found live-testing libsql==0.1.11
against a real Turso database (not assumed from docs, which gave
conflicting package names for this same thing):

1. Turso rejects `PRAGMA journal_mode=WAL` outright ("SQL not allowed
   statement") -- meaningless remotely anyway, since Turso's own
   replication is what WAL mode buys you locally. Skipped for the
   Turso path.
2. libsql raises a plain `ValueError` (message containing "UNIQUE
   constraint failed") on a UNIQUE violation, not sqlite3.IntegrityError
   -- and every store's claim-then-act / redelivery-dedup logic depends
   on catching that specific exception type (`except
   sqlite3.IntegrityError` in webhooks.py, recovery.py, executor.py).
   _TursoConnection.execute() re-raises the matching ValueError as a
   real sqlite3.IntegrityError so every existing except clause keeps
   working completely unchanged.

`.lastrowid`, `.commit()`, `.rollback()`, `?` placeholders, and chained
`.execute(...).fetchone()`/`.fetchall()` were all live-verified to behave
identically to stdlib sqlite3 -- no wrapping needed for those.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any


class _TursoCursor:
    def __init__(self, inner: Any):
        self._inner = inner

    def fetchone(self) -> Any:
        return self._inner.fetchone()

    def fetchall(self) -> Any:
        return self._inner.fetchall()

    def __iter__(self):
        # libsql's own cursor object isn't directly iterable (unlike
        # sqlite3's, which supports `for row in cursor`) -- verified live
        # against a real Turso database. fetchall() is the one iteration
        # path store.py's replay()/verify_chain() actually need.
        return iter(self._inner.fetchall())

    @property
    def lastrowid(self) -> int | None:
        return self._inner.lastrowid


class _TursoConnection:
    """sqlite3.Connection-shaped wrapper over a libsql connection -- see
    module docstring for exactly what it papers over and why."""

    def __init__(self, inner: Any):
        self._inner = inner

    def execute(self, sql: str, params: tuple = ()) -> _TursoCursor:
        try:
            return _TursoCursor(self._inner.execute(sql, params))
        except ValueError as exc:
            if "UNIQUE constraint failed" in str(exc) or "SQLITE_CONSTRAINT" in str(exc):
                raise sqlite3.IntegrityError(str(exc)) from exc
            raise

    def commit(self) -> None:
        self._inner.commit()

    def rollback(self) -> None:
        self._inner.rollback()

    def close(self) -> None:
        self._inner.close()


def connect(path: str | Path) -> sqlite3.Connection | _TursoConnection:
    """Drop-in replacement for `sqlite3.connect(path)` -- returns a real
    sqlite3.Connection against a local file (WAL mode enabled) unless
    TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are both set, in which case
    `path` is ignored and every store shares the one Turso database
    (distinct table names already keep them from colliding, same as
    they already share nothing by living in separate local files today).
    """
    turso_url = os.environ.get("TURSO_DATABASE_URL")
    turso_token = os.environ.get("TURSO_AUTH_TOKEN")
    if turso_url and turso_token:
        import libsql

        return _TursoConnection(libsql.connect(database=turso_url, auth_token=turso_token))

    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn
