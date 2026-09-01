"""LISTEN stage: turn a verified, de-duplicated webhook into SYSTEM-provenance
Facts -- the only stage allowed to do so (DEVDOC_v6 §10's stage table).

Takes an `IngestResult` that has already passed `verify_and_ingest()` (HMAC
verified, redelivery-checked) and extracts the facts a Decide/Settle step
would want: payment/mandate/refund status, tagged SYSTEM and tracing back to
the webhook's `event_id` as `source_ref`.

Callers are responsible for skipping duplicates (`result.is_duplicate`)
before calling this -- it deliberately doesn't check that itself, so it
stays a pure translation function testable on any `IngestResult`, duplicate
or not.
"""

from __future__ import annotations

from agent.ingest.webhooks import IngestResult
from agent.ledger.models import Fact, Provenance

_PAYMENT_EVENTS = frozenset({"payment.captured", "payment.failed", "payment_link.paid", "invoice.paid"})
_MANDATE_EVENTS = frozenset({"mandate.activated", "mandate.revoked", "mandate.predebit_notified"})
_REFUND_EVENTS = frozenset({"refund.processed"})

KNOWN_EVENT_TYPES: frozenset[str] = _PAYMENT_EVENTS | _MANDATE_EVENTS | _REFUND_EVENTS


class UnrecognizedWebhookEvent(Exception):
    """No fact-extraction rule exists for this event_type. Never silently
    produces zero facts for an event that might matter -- an unrecognised
    event type means either SimulatedRail emitted something new or a real
    Razorpay payload doesn't match this build's envelope assumptions
    (see docs/SIMULATOR_PROVENANCE.md)."""


def facts_from_webhook(result: IngestResult) -> list[Fact]:
    if result.event_type not in KNOWN_EVENT_TYPES:
        raise UnrecognizedWebhookEvent(f"no fact-extraction rule for event_type={result.event_type!r}")

    facts: list[Fact] = []
    payload = result.payload

    if "payment" in payload:
        entity = payload["payment"]["entity"]
        facts.append(Fact(name="payment_id", value=entity["id"], provenance=Provenance.SYSTEM, source_ref=result.event_id))
        facts.append(Fact(name="payment_status", value=entity["status"], provenance=Provenance.SYSTEM, source_ref=result.event_id))
        if "amount" in entity:
            facts.append(Fact(name="payment_amount_paise", value=entity["amount"], provenance=Provenance.SYSTEM, source_ref=result.event_id))
        if entity.get("error_code"):
            facts.append(Fact(name="payment_failure_code", value=entity["error_code"], provenance=Provenance.SYSTEM, source_ref=result.event_id))
        # A payment made against an invoice carries that invoice's id --
        # which is what lets SETTLE attribute a real capture to the thing it
        # actually paid, instead of deriving a placeholder id from the
        # payment. Only present on real invoice payments, hence the guard.
        if entity.get("invoice_id"):
            facts.append(Fact(name="invoice_id", value=entity["invoice_id"], provenance=Provenance.SYSTEM, source_ref=result.event_id))
        # Razorpay's arbitrary merchant metadata. This is where a real
        # deployment puts the ids from its own AR system, and it's the
        # documented answer to the placeholder-id limitation
        # agent/api/app.py's orchestrator has carried since it was written.
        notes = entity.get("notes") or {}
        if isinstance(notes, dict):
            for key in ("debtor_id", "invoice_id"):
                if notes.get(key) and not any(f.name == key for f in facts):
                    facts.append(Fact(name=key, value=notes[key], provenance=Provenance.SYSTEM, source_ref=result.event_id))

    if "mandate" in payload:
        entity = payload["mandate"]["entity"]
        facts.append(Fact(name="mandate_id", value=entity["id"], provenance=Provenance.SYSTEM, source_ref=result.event_id))
        if "status" in entity:
            facts.append(Fact(name="mandate_status", value=entity["status"], provenance=Provenance.SYSTEM, source_ref=result.event_id))

    if "refund" in payload:
        entity = payload["refund"]["entity"]
        facts.append(Fact(name="refund_id", value=entity["id"], provenance=Provenance.SYSTEM, source_ref=result.event_id))
        facts.append(Fact(name="refund_status", value=entity["status"], provenance=Provenance.SYSTEM, source_ref=result.event_id))

    if "payment_link" in payload:
        entity = payload["payment_link"]["entity"]
        facts.append(Fact(name="payment_link_status", value=entity["status"], provenance=Provenance.SYSTEM, source_ref=result.event_id))

    if "invoice" in payload:
        entity = payload["invoice"]["entity"]
        facts.append(Fact(name="invoice_status", value=entity["status"], provenance=Provenance.SYSTEM, source_ref=result.event_id))

    return facts
