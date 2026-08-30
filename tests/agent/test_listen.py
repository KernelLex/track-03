"""LISTEN stage: real SimulatedRail webhooks -> verify_and_ingest -> Facts, all
SYSTEM provenance, none of them ever MODEL. DEVDOC_v6 §10."""

from __future__ import annotations

import pytest

from agent.ingest.listen import UnrecognizedWebhookEvent, facts_from_webhook
from agent.ingest.webhooks import EventStore, verify_and_ingest
from agent.ledger.models import Provenance, assert_legal_provenance
from agent.rails.simulated import SimulatedRail
from agent.rails.types import LinkSpec, MandateSpec

SECRET = "listen-test-secret"


@pytest.fixture
def store(tmp_path):
    with EventStore(tmp_path / "events.db") as s:
        yield s


def ingest_latest(rail: SimulatedRail, store: EventStore):
    webhook = rail.emitted_webhooks[-1]
    return verify_and_ingest(store=store, source="simulated", body=webhook.body, signature=webhook.signature, secret=SECRET)


def test_payment_link_paid_yields_payment_and_link_facts(store):
    rail = SimulatedRail(webhook_secret=SECRET)
    link = rail.create_payment_link(LinkSpec(amount_paise=25_000, description="test"))
    rail.simulate_link_paid(link.id)

    result = ingest_latest(rail, store)
    facts = facts_from_webhook(result)
    by_name = {f.name: f.value for f in facts}

    assert by_name["payment_status"] == "captured"
    assert by_name["payment_amount_paise"] == 25_000
    assert by_name["payment_link_status"] == "paid"
    assert all(f.provenance == Provenance.SYSTEM for f in facts)
    assert all(f.source_ref == result.event_id for f in facts)


def test_invoice_paid_yields_payment_and_invoice_facts(store):
    from agent.rails.types import InvoiceSpec

    rail = SimulatedRail(webhook_secret=SECRET)
    invoice = rail.create_invoice(InvoiceSpec(amount_paise=40_000, description="test"))
    rail.simulate_invoice_paid(invoice.id)

    result = ingest_latest(rail, store)
    facts = facts_from_webhook(result)
    by_name = {f.name: f.value for f in facts}

    assert by_name["invoice_status"] == "paid"
    assert by_name["payment_status"] == "captured"


def test_failed_debit_yields_a_failure_code_fact(store, clock):
    def failure_policy(mandate_id, amount_paise):
        return ("insufficient_funds", "upi")

    rail = SimulatedRail(webhook_secret=SECRET, clock=clock, failure_policy=failure_policy)
    mandate = rail.create_mandate(MandateSpec(max_amount_paise=10_000, start_at="2026-01-01T00:00:00Z", end_at="2027-01-01T00:00:00Z"))
    rail.notify_predebit(mandate.id, 10_000, "2026-01-02T00:00:00Z", reason="scheduled")
    clock.advance(hours=24)
    rail.present_debit(mandate.id, 10_000)

    result = ingest_latest(rail, store)
    facts = facts_from_webhook(result)
    by_name = {f.name: f.value for f in facts}

    assert by_name["payment_status"] == "failed"
    assert by_name["payment_failure_code"] == "insufficient_funds"


def test_mandate_revoked_yields_a_mandate_status_fact(store):
    rail = SimulatedRail(webhook_secret=SECRET)
    mandate = rail.create_mandate(MandateSpec(max_amount_paise=10_000, start_at="2026-01-01T00:00:00Z", end_at="2027-01-01T00:00:00Z"))
    rail.revoke_mandate(mandate.id)

    result = ingest_latest(rail, store)
    facts = facts_from_webhook(result)
    by_name = {f.name: f.value for f in facts}
    assert by_name["mandate_status"] == "revoked"


def test_refund_processed_yields_refund_facts(store):
    rail = SimulatedRail(webhook_secret=SECRET)
    link = rail.create_payment_link(LinkSpec(amount_paise=15_000, description="test"))
    rail.simulate_link_paid(link.id)
    ingest_latest(rail, store)  # consume the payment_link.paid event first

    import json
    payment_id = json.loads(rail.emitted_webhooks[-1].body)["payload"]["payment"]["entity"]["id"]
    rail.create_refund(payment_id, reason="test refund")

    result = ingest_latest(rail, store)
    facts = facts_from_webhook(result)
    by_name = {f.name: f.value for f in facts}
    assert by_name["refund_status"] == "processed"


def test_every_fact_from_a_webhook_is_system_provenance_and_never_reaches_legal_computation_as_model(store):
    """Not a meaningful test of the guard (these ARE legitimately SYSTEM,
    so assert_legal_provenance should NOT raise) -- proves the negative
    space: LISTEN never mislabels a webhook-derived fact as MODEL."""
    rail = SimulatedRail(webhook_secret=SECRET)
    link = rail.create_payment_link(LinkSpec(amount_paise=10_000, description="test"))
    rail.simulate_link_paid(link.id)
    result = ingest_latest(rail, store)
    facts = facts_from_webhook(result)
    assert_legal_provenance(facts)  # must not raise -- these are legitimately SYSTEM


def test_unrecognized_event_type_raises_rather_than_producing_zero_facts_silently(store):
    import json

    from agent.rails.webhook_signing import sign

    body = json.dumps({"event": "something.brand_new", "event_id": "evt_x", "payload": {}}).encode()
    result = verify_and_ingest(store=store, source="simulated", body=body, signature=sign(body, SECRET), secret=SECRET)
    with pytest.raises(UnrecognizedWebhookEvent):
        facts_from_webhook(result)
