"""SETTLE stage: recovery_ledger, the Law 7 attribution defense. DEVDOC_v6 §9.3, §16.

Law 7: "A rupee is counted once, from a rail-confirmed object... deduplicated
by a unique database constraint, not careful code." This module is that
constraint — `attribute()` either inserts or is rejected by SQLite's own
UNIQUE(payment_id), never by an application-level "have I seen this" check.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agent.db import connect

RailTag = Literal["razorpay", "simulated"]


class NotCaptured(Exception):
    """Raised when attribution is attempted for a payment not in `captured` status.
    §16: "Not authorized, not created." A promise, an authorization, or a mandate
    created is not a recovery, and this module refuses to treat it as one."""


@dataclass(frozen=True, slots=True)
class RecoveryEntry:
    payment_id: str
    invoice_id: str
    debtor_id: str
    amount_paise: int
    rail_tag: RailTag
    arm: str | None
    recorded_at: str


class RecoveryLedger:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = connect(self.db_path)
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "RecoveryLedger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recovery_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id TEXT NOT NULL UNIQUE,
                invoice_id TEXT NOT NULL,
                debtor_id TEXT NOT NULL,
                amount_paise INTEGER NOT NULL,
                rail_tag TEXT NOT NULL,
                arm TEXT,
                recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        self._conn.commit()

    def attribute(
        self,
        *,
        payment_id: str,
        payment_status: str,
        invoice_id: str,
        debtor_id: str,
        amount_paise: int,
        rail_tag: RailTag,
        arm: str | None = None,
        recorded_at: str | None = None,
    ) -> RecoveryEntry | None:
        """Attribute a captured payment to an invoice. Returns the new RecoveryEntry,
        or None if payment_id was already attributed — a duplicate webhook delivery
        that reached SETTLE despite INGEST's own dedup (defense in depth), not an error.

        `recorded_at` defaults to now (the schema's own default) -- overriding
        it is for backfilling historical attributions with their real
        capture time (or for tests exercising agent.decide.payday_signal's
        day-of-month grouping, which needs specific dates), not a normal
        production path.
        """
        if payment_status != "captured":
            raise NotCaptured(
                f"refusing to attribute payment_id={payment_id!r} in status={payment_status!r} "
                "— only a rail-confirmed 'captured' status counts as recovered (§16)"
            )
        if amount_paise <= 0:
            raise ValueError(f"amount_paise must be positive, got {amount_paise}")

        try:
            if recorded_at is not None:
                self._conn.execute(
                    """INSERT INTO recovery_ledger
                       (payment_id, invoice_id, debtor_id, amount_paise, rail_tag, arm, recorded_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (payment_id, invoice_id, debtor_id, amount_paise, rail_tag, arm, recorded_at),
                )
            else:
                self._conn.execute(
                    """INSERT INTO recovery_ledger
                       (payment_id, invoice_id, debtor_id, amount_paise, rail_tag, arm)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (payment_id, invoice_id, debtor_id, amount_paise, rail_tag, arm),
                )
            self._conn.commit()
        except sqlite3.IntegrityError:
            self._conn.rollback()
            return None

        row = self._conn.execute(
            "SELECT payment_id, invoice_id, debtor_id, amount_paise, rail_tag, arm, recorded_at "
            "FROM recovery_ledger WHERE payment_id = ?",
            (payment_id,),
        ).fetchone()
        return RecoveryEntry(*row)

    def total_recovered_paise(
        self, *, debtor_id: str | None = None, invoice_id: str | None = None, arm: str | None = None
    ) -> int:
        query = "SELECT COALESCE(SUM(amount_paise), 0) FROM recovery_ledger WHERE 1=1"
        params: list[str] = []
        if debtor_id is not None:
            query += " AND debtor_id = ?"
            params.append(debtor_id)
        if invoice_id is not None:
            query += " AND invoice_id = ?"
            params.append(invoice_id)
        if arm is not None:
            query += " AND arm = ?"
            params.append(arm)
        return self._conn.execute(query, params).fetchone()[0]

    def all_entries(self) -> list[RecoveryEntry]:
        rows = self._conn.execute(
            "SELECT payment_id, invoice_id, debtor_id, amount_paise, rail_tag, arm, recorded_at "
            "FROM recovery_ledger ORDER BY recorded_at ASC"
        ).fetchall()
        return [RecoveryEntry(*row) for row in rows]

    def entries_for_debtor(self, debtor_id: str) -> list[RecoveryEntry]:
        rows = self._conn.execute(
            "SELECT payment_id, invoice_id, debtor_id, amount_paise, rail_tag, arm, recorded_at "
            "FROM recovery_ledger WHERE debtor_id = ? ORDER BY recorded_at ASC",
            (debtor_id,),
        ).fetchall()
        return [RecoveryEntry(*row) for row in rows]

    def entries_for_invoice(self, invoice_id: str) -> list[RecoveryEntry]:
        rows = self._conn.execute(
            "SELECT payment_id, invoice_id, debtor_id, amount_paise, rail_tag, arm, recorded_at "
            "FROM recovery_ledger WHERE invoice_id = ? ORDER BY recorded_at ASC",
            (invoice_id,),
        ).fetchall()
        return [RecoveryEntry(*row) for row in rows]
