"""Conversation state: what was said, and what is currently on the table.

Why this exists, from a real failure: the agent proposed paying the balance
on the 19th, the debtor replied "Yes it works", and the agent answered as
though a stranger had said something vague. It scored the message
`STALLING` at 0.15 confidence -- honest calibration of a message that
genuinely is ambiguous *in isolation*, which is exactly the problem. Every
reply was being diagnosed standalone, so the system could make an offer and
then fail to recognise the acceptance of it.

Two things are stored, and the second is the one that matters:

- **Turns.** The last few messages either way, so an extractor or composer
  can resolve "yes", "that works", "the second one" against what they refer
  to.
- **The outstanding proposal.** What this system last put to the debtor and
  is still waiting on. A bare "ok" means nothing on its own and means a
  great deal against a pending instalment plan.

State lives in the same store as everything else (`agent.db.connect()`), so
it is durable across restarts on Turso rather than dying with the process --
a demo that forgets its own proposal when Render cold-starts would have the
same bug in a subtler form.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from agent.db import connect


@dataclass(frozen=True, slots=True)
class Turn:
    direction: str
    """'inbound' (the debtor) or 'outbound' (this system)."""
    text: str
    at: str
    diagnosis_json: str | None = None

    @property
    def diagnosis(self) -> dict | None:
        return json.loads(self.diagnosis_json) if self.diagnosis_json else None


@dataclass(frozen=True, slots=True)
class Proposal:
    """Something this system put to the debtor and is still waiting on.

    `kind` is what was proposed ('payment_plan', 'payment_link', ...) and
    `detail_json` carries enough to act on an acceptance -- for a plan, the
    legs and dates, so "yes" can become a real instrument rather than a
    polite acknowledgement."""

    kind: str
    detail_json: str
    proposed_at: str

    @property
    def detail(self) -> dict:
        return json.loads(self.detail_json)


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happened, on any channel."""

    conversation_id: str
    channel: str | None
    kind: str
    detail_json: str | None
    at: str

    @property
    def detail(self) -> dict:
        return json.loads(self.detail_json) if self.detail_json else {}


class ConversationStore:
    def __init__(self, db_path: str = "conversation.db"):
        self.db_path = db_path
        self._conn = connect(db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                text TEXT NOT NULL,
                diagnosis_json TEXT,
                at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_turns_conversation ON conversation_turns(conversation_id)"
        )
        # One live proposal per conversation: making a second offer replaces
        # the first rather than leaving two things "on the table", which is
        # both what a person would assume and what stops an acceptance being
        # ambiguous about which offer it accepted.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_proposals (
                conversation_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                proposed_at TEXT NOT NULL
            )
            """
        )
        # Which inbound messages have already been handled. Without this a
        # restart, a redelivery, or two pollers racing would answer the same
        # message twice -- the same "the database decides, not application
        # logic" discipline agent/ingest/webhooks.py already uses.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_handled (
                conversation_id TEXT NOT NULL,
                external_id TEXT NOT NULL,
                at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE(conversation_id, external_id)
            )
            """
        )
        # Everything that happened, in order, across every channel. The
        # dashboard used to render only what its own tab had witnessed, so a
        # call placed before the page loaded, or a reply the webhook handled
        # while nothing was polling, simply did not exist as far as a viewer
        # was concerned. This is the record; the UI is a view of it.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                channel TEXT,
                kind TEXT NOT NULL,
                detail_json TEXT,
                at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_at ON conversation_events(id)")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ConversationStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- turns -------------------------------------------------------

    def record_turn(
        self, conversation_id: str, *, direction: str, text: str, diagnosis: dict | None = None,
    ) -> None:
        if direction not in ("inbound", "outbound"):
            raise ValueError(f"direction must be 'inbound' or 'outbound', got {direction!r}")
        self._conn.execute(
            "INSERT INTO conversation_turns (conversation_id, direction, text, diagnosis_json) VALUES (?, ?, ?, ?)",
            (conversation_id, direction, text, json.dumps(diagnosis) if diagnosis else None),
        )
        self._conn.commit()

    def recent_turns(self, conversation_id: str, *, limit: int = 6) -> list[Turn]:
        """Oldest-first, so it reads as a transcript. `limit` is small on
        purpose: enough to resolve a reference, not so much that a long
        history quietly grows every prompt this sends."""
        rows = self._conn.execute(
            "SELECT direction, text, at, diagnosis_json FROM conversation_turns "
            "WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [Turn(direction=r[0], text=r[1], at=r[2], diagnosis_json=r[3]) for r in reversed(rows)]

    def transcript(self, conversation_id: str, *, limit: int = 6) -> str:
        """The recent turns as plain text for a model's context block."""
        lines = []
        for turn in self.recent_turns(conversation_id, limit=limit):
            who = "Debtor" if turn.direction == "inbound" else "TrueCommit"
            lines.append(f"{who}: {turn.text}")
        return "\n".join(lines)

    # ---- the outstanding proposal ------------------------------------

    def set_proposal(self, conversation_id: str, *, kind: str, detail: dict) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO conversation_proposals (conversation_id, kind, detail_json, proposed_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(conversation_id) DO UPDATE SET "
            "kind = excluded.kind, detail_json = excluded.detail_json, proposed_at = excluded.proposed_at",
            (conversation_id, kind, json.dumps(detail), now),
        )
        self._conn.commit()

    def outstanding_proposal(self, conversation_id: str) -> Proposal | None:
        row = self._conn.execute(
            "SELECT kind, detail_json, proposed_at FROM conversation_proposals WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return Proposal(kind=row[0], detail_json=row[1], proposed_at=row[2]) if row else None

    def clear_proposal(self, conversation_id: str) -> None:
        self._conn.execute("DELETE FROM conversation_proposals WHERE conversation_id = ?", (conversation_id,))
        self._conn.commit()

    # ---- which invoice this conversation is about --------------------

    def set_focus(self, conversation_id: str, invoice_id: str) -> None:
        """Remember which invoice the debtor selected.

        Stored rather than held in memory for the same reason the proposal
        is: a cold start mid-conversation must not silently change which
        invoice "dispute this" refers to."""
        self.set_proposal(conversation_id, kind="invoice_focus", detail={"invoice_id": invoice_id})

    def focus(self, conversation_id: str) -> str | None:
        proposal = self.outstanding_proposal(conversation_id)
        if proposal is None or proposal.kind != "invoice_focus":
            return None
        return proposal.detail.get("invoice_id")

    # ---- the timeline ------------------------------------------------

    def record_event(
        self, conversation_id: str, *, kind: str, channel: str | None = None,
        detail: dict | None = None,
    ) -> None:
        """Append one thing that happened. Never raises on a bad detail
        payload -- an event that can't be recorded must not take down the
        exchange it was describing."""
        try:
            detail_json = json.dumps(detail) if detail is not None else None
        except (TypeError, ValueError):
            detail_json = json.dumps({"unserializable": repr(detail)[:500]})
        self._conn.execute(
            "INSERT INTO conversation_events (conversation_id, channel, kind, detail_json) VALUES (?, ?, ?, ?)",
            (conversation_id, channel, kind, detail_json),
        )
        self._conn.commit()

    def recent_events(self, *, conversation_id: str | None = None, limit: int = 50) -> list[Event]:
        """Oldest-first, so it reads as a timeline. Omitting
        `conversation_id` returns every channel's events interleaved --
        which is the point: a call, a WhatsApp message and a Telegram reply
        about the same invoice belong on one timeline."""
        if conversation_id is None:
            rows = self._conn.execute(
                "SELECT conversation_id, channel, kind, detail_json, at FROM conversation_events "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT conversation_id, channel, kind, detail_json, at FROM conversation_events "
                "WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        return [
            Event(conversation_id=r[0], channel=r[1], kind=r[2], detail_json=r[3], at=r[4])
            for r in reversed(rows)
        ]

    # ---- idempotency -------------------------------------------------

    def claim_message(self, conversation_id: str, external_id: str) -> bool:
        """True if this message is ours to handle, False if it was already
        handled. The UNIQUE constraint decides, not a prior read -- so a
        redelivery or a second worker can't produce a second reply."""
        try:
            self._conn.execute(
                "INSERT INTO conversation_handled (conversation_id, external_id) VALUES (?, ?)",
                (conversation_id, str(external_id)),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Only a UNIQUE violation means "already handled". Anything else
            # is a real storage failure and must not be reported as a
            # duplicate, which would silently drop the message.
            self._conn.rollback()
            return False
