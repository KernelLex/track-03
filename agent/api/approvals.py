"""The queue a human actually works from when the agent escalates.

`escalate_human` was, until now, a decision with nowhere to go. The agent
correctly refused to act, told the debtor a person would pick it up, and
then no person could — there was no list of what was waiting, and no way to
say yes or no to it. An escalation that lands nowhere is only half a
safety property: the gate stopped the wrong thing, and the right thing
never happened either.

This is the other half. Every escalation becomes a row a human can see,
with the message the agent *would* have sent attached, so approving is one
click and rejecting is one click, and either way the debtor hears back.

**Approval is recorded before the message goes out**, and the send is
recorded against it. That ordering matters: a human decision that moved
money must be reconstructable afterwards, and "we sent something and then
wrote down why" is not a record, it is a story told after the fact.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"


class ApprovalQueue:
    def __init__(self, path: str | None = None):
        self._path = path or os.environ.get("TRUECOMMIT_APPROVALS_DB", "approvals.db")
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS approvals ("
            "  id TEXT PRIMARY KEY,"
            "  created_at REAL NOT NULL,"
            "  conversation_id TEXT NOT NULL,"
            "  channel TEXT NOT NULL,"
            "  debtor_label TEXT,"
            "  invoice_id TEXT,"
            "  reason TEXT NOT NULL,"
            "  refusals TEXT,"
            "  debtor_said TEXT,"
            "  proposed_message TEXT,"
            "  mandate_links TEXT,"
            "  status TEXT NOT NULL DEFAULT 'pending',"
            "  decided_at REAL,"
            "  decided_note TEXT,"
            "  sent_ref TEXT,"
            "  send_error TEXT"
            ")"
        )
        # One open item per conversation. A debtor who writes three times
        # while waiting should not produce three identical rows for a human
        # to work through -- that is how a queue becomes noise nobody reads.
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_open_per_conversation "
            "ON approvals(conversation_id) WHERE status = 'pending'"
        )
        self._migrate()
        self._conn.commit()

    # Columns added after the table first shipped. `CREATE TABLE IF NOT
    # EXISTS` is a no-op against an existing table, so a new column in the
    # statement above reaches a fresh database and silently misses every
    # deployed one -- which is a crash on the next query, in production,
    # on a file that was working a moment earlier. Caught locally by a
    # leftover db from an earlier run; the deployed instance had exactly
    # the same stale schema and would have failed the same way.
    _ADDED_COLUMNS = (("mandate_links", "TEXT"),)

    def _migrate(self) -> None:
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(approvals)")}
        for name, sql_type in self._ADDED_COLUMNS:
            if name not in existing:
                self._conn.execute(f"ALTER TABLE approvals ADD COLUMN {name} {sql_type}")

    def open_for(
        self, *, conversation_id: str, channel: str, reason: str,
        debtor_label: str | None = None, invoice_id: str | None = None,
        refusals: list[str] | None = None, debtor_said: str | None = None,
        proposed_message: str | None = None, mandate_links: list[dict] | None = None,
    ) -> str:
        """Idempotent per conversation: a second escalation while one is
        still open refreshes the existing row rather than adding another."""
        existing = self._conn.execute(
            "SELECT id FROM approvals WHERE conversation_id = ? AND status = 'pending'",
            (conversation_id,),
        ).fetchone()
        payload = (
            channel, debtor_label, invoice_id, reason,
            json.dumps(refusals or []), debtor_said, proposed_message,
            json.dumps(mandate_links or []),
        )
        if existing:
            self._conn.execute(
                "UPDATE approvals SET channel=?, debtor_label=?, invoice_id=?, reason=?,"
                " refusals=?, debtor_said=?, proposed_message=?, mandate_links=? WHERE id=?",
                (*payload, existing["id"]),
            )
            self._conn.commit()
            return existing["id"]

        new_id = uuid.uuid4().hex[:12]
        self._conn.execute(
            "INSERT INTO approvals (id, created_at, conversation_id, channel, debtor_label,"
            " invoice_id, reason, refusals, debtor_said, proposed_message, mandate_links)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (new_id, time.time(), conversation_id, channel, debtor_label, invoice_id,
             reason, json.dumps(refusals or []), debtor_said, proposed_message,
             json.dumps(mandate_links or [])),
        )
        self._conn.commit()
        return new_id

    def get(self, approval_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return _as_dict(row) if row else None

    def decide(self, approval_id: str, *, decision: str, note: str | None = None) -> dict | None:
        """Records the decision. Deliberately does NOT send -- the caller
        owns the channel, and a store that reaches out to the network is a
        store that cannot be tested without one."""
        if decision not in (APPROVED, REJECTED):
            raise ValueError(f"decision must be {APPROVED!r} or {REJECTED!r}, not {decision!r}")
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE id = ? AND status = 'pending'", (approval_id,)
        ).fetchone()
        if row is None:
            return None
        self._conn.execute(
            "UPDATE approvals SET status=?, decided_at=?, decided_note=? WHERE id=?",
            (decision, time.time(), note, approval_id),
        )
        self._conn.commit()
        return self.get(approval_id)

    def record_send(self, approval_id: str, *, ref: str | None, error: str | None = None) -> None:
        self._conn.execute(
            "UPDATE approvals SET sent_ref=?, send_error=? WHERE id=?", (ref, error, approval_id))
        self._conn.commit()

    def pending(self) -> list[dict]:
        return [_as_dict(r) for r in self._conn.execute(
            "SELECT * FROM approvals WHERE status='pending' ORDER BY created_at ASC")]

    def recent(self, limit: int = 20) -> list[dict]:
        return [_as_dict(r) for r in self._conn.execute(
            "SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?", (limit,))]

    def clear(self) -> int:
        cursor = self._conn.execute("DELETE FROM approvals")
        self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ApprovalQueue":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _as_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for field in ("refusals", "mandate_links"):
        try:
            d[field] = json.loads(d.get(field) or "[]")
        except ValueError:
            d[field] = []
    return d
