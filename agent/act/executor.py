"""ACT stage: execute an accepted action against a Rail, idempotently, and
record it. DEVDOC_v6 §9.4, §10, §11.5.

This is the one chokepoint every rail-mutating action passes through: Law 3
(`check_bounds()` first, always), Law 6 (every outcome is tagged with the
rail that produced it), and §9.4's outbound idempotency (a retry after
timeout must not create a second payment link or mandate).

Idempotency is claim-then-act, not check-then-act: `OutboundActionStore
.claim()` is a single atomic `INSERT` gated by a `UNIQUE` constraint on the
idempotency key, so two concurrent dispatches of the same
`(debtor_id, invoice_id, action_type, decision_seq)` can't both reach the
rail — only one caller ever wins the claim. A check-then-act version (read
"has this run", then decide) would have exactly the same race Law 7's own
"not careful code" warning is about, just on the outbound side.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from agent.act.actions import ActionType
from agent.bounds.context import BoundsContext
from agent.bounds.engine import BoundsResult, check_bounds
from agent.ledger.models import LedgerEntry
from agent.ledger.store import Ledger
from agent.notify.protocol import MessageChannel
from agent.rails.protocol import Rail
from agent.rails.types import InvoiceSpec, LinkSpec, MandateDelta, MandateSpec

MESSAGE_ONLY_ACTIONS: frozenset[ActionType] = frozenset({
    ActionType.SEND_REMINDER,
    ActionType.SEND_PREDEBIT_NOTICE,
    ActionType.SEND_POSTDEBIT_NOTICE,
    ActionType.SEND_STATUTORY_NOTICE,
    ActionType.REQUEST_RECONCILIATION,
    ActionType.ESCALATE_HUMAN,
    ActionType.NO_ACTION,
    ActionType.CHECK_MANDATE_HEALTH,
})
"""No Rail call at all (§11.5's own "Rail: none" column) -- ACT records
dispatch without touching a Rail."""


def compute_idempotency_key(*, debtor_id: str, invoice_id: str, action_type: ActionType, decision_seq: int) -> str:
    raw = f"{debtor_id}|{invoice_id}|{action_type.value}|{decision_seq}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ActionRefused(Exception):
    """check_bounds() refused the proposed action. Carries the full BoundsResult
    so the caller can log every verdict, not just the first refusal (§13.3)."""

    def __init__(self, result: BoundsResult):
        self.result = result
        super().__init__(
            "action refused by check_bounds: " + "; ".join(f"{v.rule_id} ({v.reason})" for v in result.refusals)
        )


class InFlightOrStaleDuplicate(Exception):
    """The idempotency key was claimed but never finalized -- a previous
    dispatch is still in flight (or crashed mid-dispatch). A real deployment
    needs a retry-with-backoff or a claim-expiry policy here; not implemented
    in this build (see docs/LIMITATIONS.md) -- surfaced loudly rather than
    silently retried, which could double-dispatch."""


class UnknownActionType(Exception):
    """ACT has no dispatch logic for this action type. Never silently no-ops."""


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    action_type: ActionType
    idempotency_key: str
    external_ref: str | None
    rail_tag: Literal["razorpay", "simulated"] | None
    was_duplicate: bool
    detail: dict


class OutboundActionStore:
    """SQLite-backed claim table for outbound rail-mutating calls, keyed by
    idempotency_key (§9.4)."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "OutboundActionStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS outbound_actions (
                idempotency_key TEXT PRIMARY KEY,
                action_type TEXT NOT NULL,
                external_ref TEXT,
                detail_json TEXT,
                claimed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        self._conn.commit()

    def claim(self, idempotency_key: str, action_type: str) -> bool:
        """Atomically claim this key. True if newly claimed (caller must now
        dispatch and call finalize()); False if already claimed by someone else."""
        try:
            self._conn.execute(
                "INSERT INTO outbound_actions (idempotency_key, action_type) VALUES (?, ?)",
                (idempotency_key, action_type),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            self._conn.rollback()
            return False

    def finalize(self, idempotency_key: str, external_ref: str | None, detail: dict) -> None:
        self._conn.execute(
            "UPDATE outbound_actions SET external_ref = ?, detail_json = ? WHERE idempotency_key = ?",
            (external_ref, json.dumps(detail), idempotency_key),
        )
        self._conn.commit()

    def get(self, idempotency_key: str) -> tuple[str | None, dict] | None:
        """None if never claimed. Otherwise (external_ref, detail) -- external_ref
        is None while a claim is unfinalized (in flight or crashed)."""
        row = self._conn.execute(
            "SELECT external_ref, detail_json FROM outbound_actions WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        external_ref, detail_json = row
        return external_ref, (json.loads(detail_json) if detail_json else {})


def _dispatch(
    action_type: ActionType, rail: Rail, payload: dict, channel: MessageChannel | None = None
) -> tuple[str | None, dict]:
    if action_type in MESSAGE_ONLY_ACTIONS:
        if channel is not None and "to" in payload and "text" in payload:
            result = channel.send(to=payload["to"], text=payload["text"])
            return result.external_ref, {
                "message_dispatched": True, "channel": result.channel,
                "channel_status": result.status, **result.detail,
            }
        return None, {"message_dispatched": True, **{k: v for k, v in payload.items() if isinstance(v, (str, int, float, bool))}}

    if action_type == ActionType.CREATE_PAYMENT_LINK:
        link = rail.create_payment_link(LinkSpec(
            amount_paise=payload["amount_paise"], description=payload.get("description", "")
        ))
        return link.id, {"amount_paise": link.amount_paise, "status": link.status, "short_url": link.short_url}

    if action_type == ActionType.REISSUE_ARTIFACT:
        invoice = rail.create_invoice(InvoiceSpec(
            amount_paise=payload["amount_paise"], description=payload.get("description", "")
        ))
        return invoice.id, {"amount_paise": invoice.amount_paise, "status": invoice.status, "short_url": invoice.short_url}

    if action_type == ActionType.CREATE_MANDATE:
        mandate = rail.create_mandate(MandateSpec(
            max_amount_paise=payload["max_amount_paise"], start_at=payload["start_at"],
            end_at=payload["end_at"], debit_schedule=payload.get("debit_schedule", []),
        ))
        return mandate.id, {"status": mandate.status, "max_amount_paise": mandate.max_amount_paise, "short_url": mandate.short_url}

    if action_type == ActionType.RETRY_CHARGE:
        result = rail.present_debit(payload["mandate_id"], payload["amount_paise"])
        return result.payment_id, {"status": result.status, "failure_code": result.failure_code}

    if action_type == ActionType.INITIATE_REFUND:
        refund = rail.create_refund(payload["payment_id"], payload.get("reason", "unspecified"))
        return refund.id, {"status": refund.status, "amount_paise": refund.amount_paise}

    if action_type == ActionType.REVOKE_MANDATE:
        mandate = rail.revoke_mandate(payload["mandate_id"])
        return mandate.id, {"status": mandate.status}

    if action_type == ActionType.REPAIR_MANDATE:
        delta = payload["delta"] if isinstance(payload["delta"], MandateDelta) else MandateDelta(**payload["delta"])
        mandate = rail.modify_mandate(payload["mandate_id"], delta)
        return mandate.id, {"status": mandate.status, "max_amount_paise": mandate.max_amount_paise}

    raise UnknownActionType(f"ACT has no dispatch logic for {action_type.value!r}")


def _json_safe(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def execute_action(
    *,
    action_type: ActionType,
    debtor_id: str,
    invoice_id: str,
    decision_seq: int,
    bounds_context: BoundsContext,
    rail: Rail,
    outbound_store: OutboundActionStore,
    ledger: Ledger,
    payload: dict,
    actor: str = "ACT",
    channel: MessageChannel | None = None,
) -> ActionOutcome:
    """Law 4: agents coordinate only through the ledger -- every call here
    writes exactly one LedgerEntry, whether the action was accepted,
    refused, or deduped as a retry. `ledger` is required, not optional:
    a code path that could dispatch a real action without a ledger record
    is exactly the kind of gap DEVDOC_v6's Law 4 exists to close, so there
    is no "skip the ledger" parameter to reach for under time pressure.

    `channel` is optional and additive: omitting it preserves the original
    stub behaviour for MESSAGE_ONLY_ACTIONS (message_dispatched=True, no
    real send) so every existing caller and test keeps working unchanged.
    Passing a MessageChannel (agent.notify.*) makes a message-only action
    result in a real send, gated by the exact same check_bounds() call and
    idempotency claim as every other action -- there is no separate,
    lighter-touch path for messages."""
    bounds_result = check_bounds(bounds_context)
    verdict_dicts = [v.to_dict() for v in bounds_result.verdicts]

    if not bounds_result.passed:
        ledger.append(LedgerEntry(actor=actor, debtor_id=debtor_id, bounds_checks=verdict_dicts))
        raise ActionRefused(bounds_result)

    key = compute_idempotency_key(
        debtor_id=debtor_id, invoice_id=invoice_id, action_type=action_type, decision_seq=decision_seq
    )

    claimed = outbound_store.claim(key, action_type.value)
    if not claimed:
        existing = outbound_store.get(key)
        assert existing is not None  # claim() just told us someone else holds this key
        external_ref, detail = existing
        if external_ref is None:
            raise InFlightOrStaleDuplicate(
                f"idempotency_key={key} was claimed but never finalized -- a prior "
                "dispatch is in flight or crashed before completing"
            )
        was_duplicate = True
    else:
        external_ref, detail = _dispatch(action_type, rail, payload, channel)
        outbound_store.finalize(key, external_ref, detail)
        was_duplicate = False

    rail_tag = getattr(rail, "rail_tag", None)
    ledger.append(LedgerEntry(
        actor=actor, debtor_id=debtor_id, bounds_checks=verdict_dicts,
        action={
            "type": action_type.value,
            "payload": _json_safe(payload),
            "bounds_context_snapshot": bounds_context.to_dict(),
        },
        idempotency_key=key,
        rail_tag=rail_tag,
        outcome={"external_ref": external_ref, "was_duplicate": was_duplicate, "detail": _json_safe(detail)},
    ))

    return ActionOutcome(
        action_type=action_type, idempotency_key=key, external_ref=external_ref,
        rail_tag=rail_tag, was_duplicate=was_duplicate, detail=detail,
    )
