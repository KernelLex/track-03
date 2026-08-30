"""Append-only, hash-chained SQLite ledger. The only bus between stages (Law 4). DEVDOC_v6 §15.

`ts` and `seq` are assigned here, at append time, and folded into the hashed
payload — never accepted from the caller — so neither can be forged or
back-dated without breaking the chain (§15's tamper-evidence contract).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from agent.ledger.models import LedgerEntry

GENESIS_HASH = "0" * 64


class ChainIntegrityError(Exception):
    """Raised when prev_hash continuity or a row's hash breaks. Names the exact seq. §11.7, §15."""


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(prev_hash: str, body: dict[str, Any]) -> str:
    digest_input = _canonical_json({"prev_hash": prev_hash, "body": body})
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


class Ledger:
    """SQLite-backed, append-only, hash-chained ledger.

    One physical chain across every debtor — seq is global, interleaved by
    arrival order. `replay(debtor_id, ...)` filters that single chain rather
    than maintaining a separate chain per debtor, so the tamper-evidence and
    ordering guarantees are system-wide, not per-subject.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ledger (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                debtor_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                body_json TEXT NOT NULL,
                hash TEXT NOT NULL
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_debtor ON ledger(debtor_id)")
        self._conn.commit()

    def _last(self) -> tuple[int, str, str] | None:
        row = self._conn.execute("SELECT seq, hash, ts FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
        return (row[0], row[1], row[2]) if row else None

    def append(self, entry: LedgerEntry) -> LedgerEntry:
        """Assign seq/ts/prev_hash/hash and persist. Returns the finalized entry.

        `ts` is enforced strictly increasing: if the wall clock hasn't advanced
        past the previous row's `ts` (real on Windows, where clock resolution
        can be coarser than the microsecond precision `isoformat` implies —
        caught by test_replay_until_timestamp_excludes_later_entries), it is
        bumped forward by one microsecond instead. Without this, two entries
        can share a `ts`, and `replay(..., until=T)` can no longer tell them
        apart — "state as of T" stops being a well-defined question.
        """
        last = self._last()
        prev_hash = last[1] if last else GENESIS_HASH
        next_seq = (last[0] + 1) if last else 1
        now = datetime.now(timezone.utc)
        if last is not None:
            last_ts = datetime.fromisoformat(last[2])
            if now <= last_ts:
                now = last_ts + timedelta(microseconds=1)
        ts = now.isoformat(timespec="microseconds")

        body = entry.payload()
        body["seq"] = next_seq
        body["ts"] = ts
        entry_hash = compute_hash(prev_hash, body)

        self._conn.execute(
            "INSERT INTO ledger (seq, debtor_id, ts, prev_hash, body_json, hash) VALUES (?, ?, ?, ?, ?, ?)",
            (next_seq, entry.debtor_id, ts, prev_hash, _canonical_json(body), entry_hash),
        )
        self._conn.commit()
        return replace(entry, seq=next_seq, ts=ts, prev_hash=prev_hash, hash=entry_hash)

    def all_entries(self) -> Iterator[LedgerEntry]:
        cursor = self._conn.execute("SELECT seq, prev_hash, body_json, hash FROM ledger ORDER BY seq ASC")
        for seq, prev_hash, body_json, hash_ in cursor:
            yield LedgerEntry.from_row(seq=seq, prev_hash=prev_hash, hash=hash_, body=json.loads(body_json))

    def verify_chain(self) -> None:
        """Raise ChainIntegrityError naming the exact seq where continuity or a hash breaks. §15."""
        expected_prev = GENESIS_HASH
        cursor = self._conn.execute("SELECT seq, prev_hash, body_json, hash FROM ledger ORDER BY seq ASC")
        for seq, prev_hash, body_json, hash_ in cursor:
            if prev_hash != expected_prev:
                raise ChainIntegrityError(
                    f"chain break at seq={seq}: expected prev_hash={expected_prev!r}, found {prev_hash!r}"
                )
            body = json.loads(body_json)
            recomputed = compute_hash(prev_hash, body)
            if recomputed != hash_:
                raise ChainIntegrityError(
                    f"chain break at seq={seq}: stored hash {hash_!r} does not match recomputed "
                    f"hash {recomputed!r} — payload was tampered with after being written"
                )
            expected_prev = hash_

    def replay(self, debtor_id: str, until: str | None = None) -> "ReplayResult":
        """Reconstruct one debtor's history by folding their slice of the (already-verified) chain.

        `until` is an ISO8601 timestamp cutoff (`ts <= until`), inclusive — reconstructs
        state as of a point in time, not just "as of now". Caller is expected to have
        called verify_chain() first when the result will inform a decision; replay()
        itself does not re-verify on every call, to keep it cheap for read-only UI use.
        """
        query = "SELECT seq, prev_hash, body_json, hash FROM ledger WHERE debtor_id = ?"
        params: list[Any] = [debtor_id]
        if until is not None:
            query += " AND ts <= ?"
            params.append(until)
        query += " ORDER BY seq ASC"

        entries = tuple(
            LedgerEntry.from_row(seq=seq, prev_hash=prev_hash, hash=hash_, body=json.loads(body_json))
            for seq, prev_hash, body_json, hash_ in self._conn.execute(query, params)
        )
        return ReplayResult(debtor_id=debtor_id, entries=entries)


class ReplayResult:
    """The debtor's ledger slice, folded into the derived views callers actually want."""

    __slots__ = ("debtor_id", "entries")

    def __init__(self, debtor_id: str, entries: tuple[LedgerEntry, ...]):
        self.debtor_id = debtor_id
        self.entries = entries

    @property
    def last_seq(self) -> int | None:
        return self.entries[-1].seq if self.entries else None

    @property
    def current_state(self) -> str | None:
        """The most recent `outcome["debtor_state_after"]` — the state machine's job to write, not the ledger's."""
        for entry in reversed(self.entries):
            if entry.outcome and "debtor_state_after" in entry.outcome:
                return entry.outcome["debtor_state_after"]
        return None

    @property
    def recovered_paise(self) -> int:
        """Sum of outcome['recovered_paise'] across this debtor's entries.

        Law 7's dedup guarantee comes from the recovery_ledger UNIQUE(payment_id)
        constraint upstream (§9.3) — this is a pure fold over what already landed
        in the ledger, not a second place dedup could be gotten wrong.
        """
        return sum(e.outcome.get("recovered_paise", 0) for e in self.entries if e.outcome)
