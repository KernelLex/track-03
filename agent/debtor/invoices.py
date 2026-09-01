"""The invoices a debtor actually has, and what has happened to each.

Until now the conversation was about one hardcoded invoice: `SCENARIOS`
held `INV-2201`, and every reply was implicitly about it. That is fine for
demonstrating a chase and useless for the thing a debtor most often wants,
which is not to negotiate at all -- it is to see what they owe, confirm
what they have already paid, and deal with one specific line item.

A real accounts-receivable counterparty has several invoices open at once,
and the most common support question in that world is "which of these is
still outstanding?". Answering it well removes work from a human queue
without chasing anyone, which is the same argument this project makes about
diagnosis: the cheapest recovery is the one that never needed a chase.

**Status is a fact, not a claim.** `paid` is set by a rail-confirmed
capture, the same Law 7 standard `RecoveryLedger.attribute()` enforces --
never because a debtor said so. `disputed` is set when a dispute is raised
and is what freezes automated contact on that line. Nothing here lets a
message change what is owed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from agent.clock import business_today
from agent.db import connect

OUTSTANDING = "outstanding"
PAID = "paid"
DISPUTED = "disputed"
SCHEDULED = "scheduled"

STATUS_LABEL = {
    OUTSTANDING: "due",
    PAID: "paid",
    DISPUTED: "disputed -- with a person",
    SCHEDULED: "scheduled",
}


@dataclass(frozen=True, slots=True)
class Invoice:
    debtor_id: str
    invoice_id: str
    amount_paise: int
    due_date: str
    status: str
    note: str = ""

    @property
    def is_open(self) -> bool:
        """Open means it still needs something to happen. A disputed
        invoice is not open for chasing -- a person has it."""
        return self.status in (OUTSTANDING, SCHEDULED)

    def days_overdue(self, today: date | None = None) -> int:
        today = today or business_today()
        try:
            due = date.fromisoformat(self.due_date)
        except ValueError:  # pragma: no cover -- dates are written as ISO
            return 0
        return max(0, (today - due).days)


class InvoiceStore:
    def __init__(self, db_path: str = "debtors.db"):
        # Same database as the debtor register: an invoice belongs to a
        # debtor, and splitting them across files would make the join a
        # cross-database problem for no benefit.
        self.db_path = db_path
        self._conn = connect(db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                debtor_id TEXT NOT NULL,
                invoice_id TEXT NOT NULL,
                amount_paise INTEGER NOT NULL,
                due_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'outstanding',
                note TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE(debtor_id, invoice_id)
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "InvoiceStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def upsert(self, invoice: Invoice) -> None:
        self._conn.execute(
            "INSERT INTO invoices (debtor_id, invoice_id, amount_paise, due_date, status, note) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(debtor_id, invoice_id) DO UPDATE SET "
            "amount_paise = excluded.amount_paise, due_date = excluded.due_date, "
            "status = excluded.status, note = excluded.note",
            (invoice.debtor_id, invoice.invoice_id, invoice.amount_paise,
             invoice.due_date, invoice.status, invoice.note),
        )
        self._conn.commit()

    def add_if_absent(self, invoice: Invoice) -> bool:
        """Seeding helper: never overwrite an invoice whose status a real
        payment or dispute has already moved."""
        try:
            self._conn.execute(
                "INSERT INTO invoices (debtor_id, invoice_id, amount_paise, due_date, status, note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (invoice.debtor_id, invoice.invoice_id, invoice.amount_paise,
                 invoice.due_date, invoice.status, invoice.note),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            self._conn.rollback()
            return False

    def _row(self, r) -> Invoice:
        return Invoice(debtor_id=r[0], invoice_id=r[1], amount_paise=r[2],
                       due_date=r[3], status=r[4], note=r[5])

    _COLUMNS = "SELECT debtor_id, invoice_id, amount_paise, due_date, status, note FROM invoices"

    def for_debtor(self, debtor_id: str) -> list[Invoice]:
        """Open invoices first, oldest due date first -- the order someone
        asking "what do I owe" wants to read them in."""
        rows = self._conn.execute(
            f"{self._COLUMNS} WHERE debtor_id = ? "
            "ORDER BY CASE status WHEN 'paid' THEN 1 ELSE 0 END, due_date",
            (debtor_id,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def get(self, debtor_id: str, invoice_id: str) -> Invoice | None:
        row = self._conn.execute(
            f"{self._COLUMNS} WHERE debtor_id = ? AND invoice_id = ?",
            (debtor_id, invoice_id),
        ).fetchone()
        return self._row(row) if row else None

    def set_status(self, debtor_id: str, invoice_id: str, status: str, *, note: str = "") -> bool:
        """Returns False if there was no such invoice, so a caller can tell
        "not found" from "already in that state"."""
        if status not in STATUS_LABEL:
            raise ValueError(f"unknown invoice status {status!r}")
        cursor = self._conn.execute(
            "UPDATE invoices SET status = ?, note = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE debtor_id = ? AND invoice_id = ?",
            (status, note, debtor_id, invoice_id),
        )
        self._conn.commit()
        return bool(getattr(cursor, "rowcount", 0))

    def mark_paid_by_capture(self, debtor_id: str, invoice_id: str, *, payment_id: str) -> bool:
        """Law 7's standard: only a rail-confirmed capture marks an invoice
        paid. A debtor saying they have paid is a claim to check, not a
        status change -- `ALREADY_PAID_UNRECONCILED` exists as a diagnosis
        class precisely because those two are different things."""
        return self.set_status(debtor_id, invoice_id, PAID, note=f"captured {payment_id}")

    def total_outstanding_paise(self, debtor_id: str) -> int:
        return sum(i.amount_paise for i in self.for_debtor(debtor_id) if i.is_open)
