"""Telegram chats that are allowed to drive the demo conversation.

The inbound webhook has always guarded to a single configured chat id, and
fails closed when it is unset -- a stranger who finds the bot must not be
able to make the system answer them, spend a real model call, or appear in
the timeline as though the demo's own debtor had written. That guard is
right, and `docs/WHAT_BROKE.md` #25 is the record of what happened when an
earlier version of it failed open.

But it also means only one person can ever try the two-way flow, which is
useless when several judges want to. This is the narrow relaxation: a chat
id becomes allowed only when someone deliberately enters it in the
dashboard and triggers a send to it, and only for a limited window.

**Why entering a chat id is not a way to spam strangers.** Telegram itself
refuses to let a bot message a chat that has not messaged the bot first.
So an id typed in here can only receive anything if that person has
already opened a conversation with this bot -- the platform enforces the
consent, not this file. What the allowlist adds is the *inbound* half:
having opted in by messaging the bot and asked for a send, that person's
replies are now answered.

Entries expire. A demo audience is transient, and an allowlist that only
grows is one that eventually cannot be reasoned about.
"""

from __future__ import annotations

import os
import sqlite3
import time

DEFAULT_TTL_SECONDS = 6 * 60 * 60
"""Six hours -- long enough for a judging session, short enough that a
chat id typed in today is not still privileged next week."""


class TelegramAllowlist:
    def __init__(self, path: str | None = None, *, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self._path = path or os.environ.get("TRUECOMMIT_ALLOWLIST_DB", "demo_allowlist.db")
        self._ttl = ttl_seconds
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS allowed_chats ("
            "  chat_id TEXT PRIMARY KEY,"
            "  added_at REAL NOT NULL"
            ")"
        )
        self._conn.commit()

    def allow(self, chat_id: str) -> None:
        """Idempotent, and refreshes the clock: someone still actively
        demoing should not be timed out mid-conversation."""
        self._conn.execute(
            "INSERT INTO allowed_chats (chat_id, added_at) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET added_at = excluded.added_at",
            (str(chat_id), time.time()),
        )
        self._conn.commit()

    def is_allowed(self, chat_id: str) -> bool:
        row = self._conn.execute(
            "SELECT added_at FROM allowed_chats WHERE chat_id = ?", (str(chat_id),)
        ).fetchone()
        if row is None:
            return False
        if time.time() - row[0] > self._ttl:
            # Expired entries are removed on read rather than by a sweeper.
            # There is no background job here to run one, and an expired row
            # that lingers is a row that could be misread as permission.
            self.revoke(chat_id)
            return False
        return True

    def revoke(self, chat_id: str) -> None:
        self._conn.execute("DELETE FROM allowed_chats WHERE chat_id = ?", (str(chat_id),))
        self._conn.commit()

    def clear(self) -> int:
        cursor = self._conn.execute("DELETE FROM allowed_chats")
        self._conn.commit()
        return cursor.rowcount

    def active(self) -> list[str]:
        cutoff = time.time() - self._ttl
        return [r[0] for r in self._conn.execute(
            "SELECT chat_id FROM allowed_chats WHERE added_at >= ? ORDER BY added_at DESC", (cutoff,)
        )]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "TelegramAllowlist":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
