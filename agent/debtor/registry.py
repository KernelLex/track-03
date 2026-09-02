"""Who owes what, and what their promises have been worth.

Until now the demo had exactly one implicit debtor: whoever happened to be
on the other end of `DEMO_CONTACT_TELEGRAM_CHAT_ID`. Every conversation
was keyed on a channel id, every score was the `1.0` default, and there was
no way to ask "what has this debtor done before" -- which is the question
`promise_credibility` was designed around and never able to answer.

This is the register. It holds real debtors (the person testing this
system, on their own Telegram chat) alongside seeded ones whose promise
histories are declared rather than lived, and it is careful to keep those
two things distinguishable: `is_seeded` is stored per debtor, and every
API that returns a debtor returns it too. A seeded history is a fixture for
showing what the scoring does across bands; presenting it as evidence of
real behaviour would be the same overclaim `docs/RESULTS.md` already
refuses to make about simulated recovery.

State lives in `agent.db.connect()` like everything else, so it survives
the Render cold starts that a demo actually hits.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone

from agent.clock import business_today

from agent.db import connect
from agent.debtor.score import DebtorTerms, PromiseOutcome, terms_for


class ChannelRefTaken(Exception):
    """Two debtors cannot share one channel address -- see the unique index
    on `debtors.channel_ref` for what goes wrong when they do."""


@dataclass(frozen=True, slots=True)
class Debtor:
    id: str
    display_name: str
    channel: str
    channel_ref: str
    """The Telegram chat id / phone number this debtor is reachable on --
    the join between the register and `ConversationStore`'s history."""
    invoice_id: str
    invoice_amount_paise: int
    is_seeded: bool
    note: str = ""
    created_at: str = ""


class DebtorRegistry:
    def __init__(self, db_path: str = "debtors.db"):
        self.db_path = db_path
        self._conn = connect(db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS debtors (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                channel TEXT NOT NULL,
                channel_ref TEXT NOT NULL,
                invoice_id TEXT NOT NULL,
                invoice_amount_paise INTEGER NOT NULL,
                is_seeded INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        # One row per promise. `outcome` starts 'pending' and is resolved by
        # a rail-confirmed capture or by the date passing -- never by a
        # model's read of whether the debtor sounded sincere.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS promise_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                debtor_id TEXT NOT NULL,
                invoice_id TEXT NOT NULL,
                promised_amount_paise INTEGER NOT NULL,
                promised_date TEXT NOT NULL,
                outcome TEXT NOT NULL DEFAULT 'pending',
                payment_id TEXT,
                recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        # One debtor per channel address. Two debtors sharing a Telegram
        # chat id is not a data-modelling nicety -- it silently misroutes:
        # a promise gets recorded against one and settled against the other,
        # so a real payment improves the wrong debtor's score. Caught by
        # test_a_real_capture_keeps_an_open_promise.
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_debtors_channel_ref ON debtors(channel_ref)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_promises_debtor ON promise_outcomes(debtor_id, id)"
        )
        # A capture must settle a promise exactly once, for the same reason
        # RecoveryLedger has UNIQUE(payment_id): a redelivered webhook must
        # not be able to improve someone's score twice.
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_promises_payment "
            "ON promise_outcomes(payment_id) WHERE payment_id IS NOT NULL"
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "DebtorRegistry":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- debtors -----------------------------------------------------

    def upsert(self, debtor: Debtor) -> None:
        """Raises ChannelRefTaken if another debtor already holds this
        channel address -- surfaced rather than swallowed, because the
        alternative is a conversation quietly attributed to the wrong
        person."""
        try:
            self._upsert(debtor)
        except sqlite3.IntegrityError as exc:
            raise ChannelRefTaken(
                f"channel_ref {debtor.channel_ref!r} already belongs to another debtor"
            ) from exc

    def _upsert(self, debtor: Debtor) -> None:
        self._conn.execute(
            "INSERT INTO debtors (id, display_name, channel, channel_ref, invoice_id, "
            "invoice_amount_paise, is_seeded, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET display_name = excluded.display_name, "
            "channel = excluded.channel, channel_ref = excluded.channel_ref, "
            "invoice_id = excluded.invoice_id, invoice_amount_paise = excluded.invoice_amount_paise, "
            "is_seeded = excluded.is_seeded, note = excluded.note",
            (debtor.id, debtor.display_name, debtor.channel, debtor.channel_ref,
             debtor.invoice_id, debtor.invoice_amount_paise, int(debtor.is_seeded), debtor.note),
        )
        self._conn.commit()

    def _row_to_debtor(self, r) -> Debtor:
        return Debtor(
            id=r[0], display_name=r[1], channel=r[2], channel_ref=r[3], invoice_id=r[4],
            invoice_amount_paise=r[5], is_seeded=bool(r[6]), note=r[7], created_at=r[8],
        )

    _COLUMNS = ("SELECT id, display_name, channel, channel_ref, invoice_id, "
                "invoice_amount_paise, is_seeded, note, created_at FROM debtors")

    def all_debtors(self) -> list[Debtor]:
        rows = self._conn.execute(f"{self._COLUMNS} ORDER BY is_seeded, id").fetchall()
        return [self._row_to_debtor(r) for r in rows]

    def debtor(self, debtor_id: str) -> Debtor | None:
        row = self._conn.execute(f"{self._COLUMNS} WHERE id = ?", (debtor_id,)).fetchone()
        return self._row_to_debtor(row) if row else None

    def by_channel_ref(self, channel_ref: str) -> Debtor | None:
        """The lookup the conversation path needs: a Telegram chat id or a
        phone number arrives, and the question is whose record it is."""
        row = self._conn.execute(f"{self._COLUMNS} WHERE channel_ref = ?", (channel_ref,)).fetchone()
        return self._row_to_debtor(row) if row else None

    # ---- promises ----------------------------------------------------

    def record_promise(
        self, debtor_id: str, *, invoice_id: str, amount_paise: int, promised_date: str,
        outcome: str = "pending", payment_id: str | None = None,
    ) -> None:
        try:
            self._conn.execute(
                "INSERT INTO promise_outcomes (debtor_id, invoice_id, promised_amount_paise, "
                "promised_date, outcome, payment_id) VALUES (?, ?, ?, ?, ?, ?)",
                (debtor_id, invoice_id, amount_paise, promised_date, outcome, payment_id),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            # A capture already settled a promise. Not an error -- a
            # redelivered webhook must not count twice.
            self._conn.rollback()

    def settle_promise(self, debtor_id: str, *, payment_id: str, invoice_id: str | None = None) -> bool:
        """A rail-confirmed capture keeps the debtor's oldest open promise.

        Oldest-first because that is the one the payment most plausibly
        answers, and because leaving the oldest open forever would let a
        debtor accrue pending promises indefinitely while paying only the
        newest. Returns False when there was nothing open to settle.
        """
        row = None
        if invoice_id:
            row = self._conn.execute(
                "SELECT id FROM promise_outcomes WHERE debtor_id = ? AND invoice_id = ? "
                "AND outcome = 'pending' ORDER BY id LIMIT 1", (debtor_id, invoice_id)).fetchone()
        if row is None:
            # The rail's invoice id and the merchant's invoice reference are
            # different namespaces. A real capture arrived carrying
            # `inv_TWte5TwAYXxtq8` -- Razorpay's own id -- while the promise
            # was recorded against `INV-2201`, the merchant's reference, so
            # the scoped lookup matched nothing, the promise stayed pending,
            # its date passed, and it was scored as broken. A debtor who
            # actually paid was recorded as having broken their word.
            #
            # Falling back to the oldest open promise is what the docstring
            # above always described. Scoping by invoice is an optimisation
            # for the case where the two ids genuinely align (a merchant
            # that propagates its own reference through the payment's
            # notes); it must never be the only way to match.
            row = self._conn.execute(
                "SELECT id FROM promise_outcomes WHERE debtor_id = ? AND outcome = 'pending' "
                "ORDER BY id LIMIT 1", (debtor_id,)).fetchone()
        if row is None:
            return False
        self._conn.execute(
            "UPDATE promise_outcomes SET outcome = 'kept', payment_id = ? WHERE id = ?",
            (payment_id, row[0]),
        )
        self._conn.commit()
        return True

    def expire_overdue_promises(self, debtor_id: str, *, today: date | None = None) -> int:
        """A promised date that passed with no capture is a broken promise.

        Time passing is what resolves this, not a judgement about the
        debtor -- which is exactly the property that makes the resulting
        score defensible."""
        today = today or business_today()
        rows = self._conn.execute(
            "SELECT id, promised_date FROM promise_outcomes WHERE debtor_id = ? AND outcome = 'pending'",
            (debtor_id,),
        ).fetchall()
        broken = 0
        for pid, promised in rows:
            try:
                if date.fromisoformat(promised) < today:
                    self._conn.execute(
                        "UPDATE promise_outcomes SET outcome = 'broken' WHERE id = ?", (pid,))
                    broken += 1
            except ValueError:  # pragma: no cover -- dates are written as ISO
                continue
        if broken:
            self._conn.commit()
        return broken

    def clear_promises(self, debtor_id: str) -> int:
        """Forget a debtor's promise history. Returns how many rows went.

        Only ever called from the secret-gated demo reset. This deletes a
        record of real events, which is why nothing else may call it and why
        the reset does not do it by default -- but a record the system got
        wrong (WHAT_BROKE #26 scored a debtor who had paid as having broken
        their word) needs some way to be corrected."""
        rows = self._conn.execute(
            "SELECT COUNT(*) FROM promise_outcomes WHERE debtor_id = ?", (debtor_id,)
        ).fetchone()
        count = int(rows[0]) if rows else 0
        self._conn.execute("DELETE FROM promise_outcomes WHERE debtor_id = ?", (debtor_id,))
        self._conn.commit()
        return count

    def outcomes_for(self, debtor_id: str) -> list[PromiseOutcome]:
        rows = self._conn.execute(
            "SELECT invoice_id, promised_amount_paise, promised_date, outcome, recorded_at, payment_id "
            "FROM promise_outcomes WHERE debtor_id = ? ORDER BY id", (debtor_id,),
        ).fetchall()
        return [
            PromiseOutcome(invoice_id=r[0], promised_amount_paise=r[1],
                           promised_date=date.fromisoformat(r[2]), outcome=r[3],
                           recorded_at=r[4], payment_id=r[5])
            for r in rows
        ]

    def terms(self, debtor_id: str) -> DebtorTerms:
        return terms_for(self.outcomes_for(debtor_id))
