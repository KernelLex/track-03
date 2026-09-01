"""INGEST stage: verify before parsing, then de-duplicate. DEVDOC_v6 §9.2, §9.3, §10.

Ordering matters: `verify_and_ingest` never inspects `body` as JSON until the
raw bytes have passed HMAC verification, so an attacker can't use a forged,
signature-rejected delivery to pre-empt (burn) the real event's dedup slot.

The envelope contract assumed here — top-level `event` and `event_id` keys —
is the one `SimulatedRail._emit()` produces (agent/rails/simulated.py). A
real RazorpayRail webhook route needs its own adapter in front of this
function once Razorpay's exact live payload shape is verified against
captured fixtures (§5.5) — see docs/SIMULATOR_PROVENANCE.md.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from agent.rails.webhook_signing import verify


class SignatureInvalid(Exception):
    """Raised when a webhook's HMAC signature doesn't verify. The caller must not
    proceed to parse or act on `body` when this is raised."""


class MalformedWebhook(Exception):
    """Raised when a signature-verified body doesn't carry the expected envelope
    (`event`, `event_id`) — a shape problem, not a trust problem."""


@dataclass(frozen=True, slots=True)
class IngestResult:
    event_id: str
    source: str
    event_type: str
    is_duplicate: bool
    """True if this (source, event_id) was already recorded — caller returns 200
    and stops (§9.3): a redelivery must be acknowledged, never reprocessed."""
    payload: dict
    """The parsed envelope's payload — only meaningful when is_duplicate is False;
    a duplicate must not be re-acted-on even though the payload is still returned
    for logging."""


class EventStore:
    """SQLite-backed dedup table for inbound events. UNIQUE(source, event_id) is the
    actual defense — the insert either succeeds or is rejected by the database,
    never by application-level "have I seen this" logic that can race under
    concurrent delivery."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                event_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE(source, event_id)
            )
            """
        )
        self._conn.commit()

    def record(self, source: str, event_id: str, event_type: str) -> bool:
        """Try to record this event. True if newly recorded, False if already present."""
        try:
            self._conn.execute(
                "INSERT INTO events (source, event_id, event_type) VALUES (?, ?, ?)",
                (source, event_id, event_type),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            self._conn.rollback()
            return False

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def verify_and_ingest(
    *,
    store: EventStore,
    source: str,
    body: bytes,
    signature: str,
    secret: str,
    event_id_header: str | None = None,
) -> IngestResult:
    """The one path every rail's webhooks go through (§9.2, §10's INGEST row).

    1. Verify the raw bytes against the signature. Raise before touching content.
    2. Parse the envelope.
    3. Record (source, event_id) — the database, not application logic, decides
       whether this is a redelivery.

    `event_id_header` is Razorpay's real delivery shape: the event id arrives
    only as the `x-razorpay-event-id` header, never as a body field (verified
    against Razorpay's own webhook docs) — takes priority over any body
    `event_id` when given. Falls back to the body field for
    `SimulatedRail._emit()`'s synthetic envelope, which has no such header.
    """
    if not verify(body, signature, secret):
        raise SignatureInvalid(f"webhook signature verification failed for source={source!r}")

    try:
        envelope = json.loads(body)
        event_id = event_id_header or envelope["event_id"]
        event_type = envelope["event"]
        payload = envelope.get("payload", {})
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise MalformedWebhook(f"signature-verified body from source={source!r} is not a valid envelope: {exc}") from exc

    newly_recorded = store.record(source, event_id, event_type)
    return IngestResult(
        event_id=event_id, source=source, event_type=event_type,
        is_duplicate=not newly_recorded, payload=payload,
    )
