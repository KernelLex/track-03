"""The reversal path — the inverse of every money-moving action. DEVDOC_v6 §11.6, Law 9.

"An agent that can spend but not un-spend is a half-built agent." Three rules
this module exists to uphold:

1. A reversal is not a negative recovery — tracked in its own table, reported
   as recovered/reversed/net, never silently combined into one number.
2. The ledger links them: a reversal's ledger entry carries `reverses_seq`
   pointing at the original action's seq.
3. Erroneous debits are a Tier-1 safety metric, reported as a count with a
   time-to-reversal distribution.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from agent.act.actions import ActionType


class ReversalGate(str, Enum):
    HUMAN = "human"
    AUTONOMOUS = "autonomous"


@dataclass(frozen=True, slots=True)
class ReversalSpec:
    forward: ActionType
    inverse: str
    """Usually an ActionType value, but send_statutory_notice's inverse
    (send_correction_notice) isn't itself a typed action in §11.5's table —
    kept as a plain str rather than forcing a fictitious ActionType member."""
    gate: ReversalGate
    note: str


REVERSAL_MAP: dict[ActionType, ReversalSpec] = {
    ActionType.RETRY_CHARGE: ReversalSpec(
        forward=ActionType.RETRY_CHARGE,
        inverse=ActionType.INITIATE_REFUND.value,
        gate=ReversalGate.HUMAN,
        note="a captured payment is refunded via initiate_refund(payment_id, reason)",
    ),
    ActionType.CREATE_MANDATE: ReversalSpec(
        forward=ActionType.CREATE_MANDATE,
        inverse=ActionType.REVOKE_MANDATE.value,
        gate=ReversalGate.HUMAN,
        note="human-gated in general; AUTONOMOUS specifically on debtor opt-out this "
             "cycle, via reversal_gate_for() below — refusing to reverse on opt-out is "
             "itself a violation under the 2026 framework (§11.6)",
    ),
    ActionType.REISSUE_ARTIFACT: ReversalSpec(
        forward=ActionType.REISSUE_ARTIFACT,
        inverse=ActionType.REISSUE_ARTIFACT.value,
        gate=ReversalGate.AUTONOMOUS,
        note="reverting an artifact moves no money — reissue with the prior corrections",
    ),
    ActionType.SEND_STATUTORY_NOTICE: ReversalSpec(
        forward=ActionType.SEND_STATUTORY_NOTICE,
        inverse="send_correction_notice",
        gate=ReversalGate.HUMAN,
        note="a withdrawn legal claim needs a written trail",
    ),
}


class NoReversalDefined(Exception):
    """Raised when a money-moving action has no entry in REVERSAL_MAP — Law 9
    violated by omission. A missing reversal must be visible, not assumed."""


def reversal_gate_for(action_type: ActionType, *, debtor_opted_out_this_cycle: bool = False) -> ReversalGate:
    spec = REVERSAL_MAP.get(action_type)
    if spec is None:
        raise NoReversalDefined(f"no reversal is defined for {action_type.value!r}")
    if action_type == ActionType.CREATE_MANDATE and debtor_opted_out_this_cycle:
        return ReversalGate.AUTONOMOUS
    return spec.gate


@dataclass(frozen=True, slots=True)
class ReversalEntry:
    id: int
    original_action_type: str
    reverses_seq: int
    """Points at the original action's ledger seq (§11.6 rule 2) — replay()
    reconstructs both the error and the correction from this link."""
    amount_paise: int
    reason: str
    recorded_at: str


class ReversalsLedger:
    """A separate table from recovery_ledger, on purpose (§11.6 rule 1): a
    reversal is never netted into the recovered total by this module. Whoever
    reports a headline number is responsible for showing recovered/reversed/net
    as three figures, not one."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ReversalsLedger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reversals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_action_type TEXT NOT NULL,
                reverses_seq INTEGER NOT NULL,
                amount_paise INTEGER NOT NULL,
                reason TEXT NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        self._conn.commit()

    def record(self, *, original_action_type: str, reverses_seq: int, amount_paise: int, reason: str) -> ReversalEntry:
        if amount_paise <= 0:
            raise ValueError("amount_paise must be positive")
        cursor = self._conn.execute(
            "INSERT INTO reversals (original_action_type, reverses_seq, amount_paise, reason) VALUES (?, ?, ?, ?)",
            (original_action_type, reverses_seq, amount_paise, reason),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id, original_action_type, reverses_seq, amount_paise, reason, recorded_at "
            "FROM reversals WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        return ReversalEntry(*row)

    def total_reversed_paise(self) -> int:
        return self._conn.execute("SELECT COALESCE(SUM(amount_paise), 0) FROM reversals").fetchone()[0]

    def all_entries(self) -> list[ReversalEntry]:
        rows = self._conn.execute(
            "SELECT id, original_action_type, reverses_seq, amount_paise, reason, recorded_at "
            "FROM reversals ORDER BY recorded_at ASC"
        ).fetchall()
        return [ReversalEntry(*row) for row in rows]
