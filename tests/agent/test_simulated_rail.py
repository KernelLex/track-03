"""SimulatedRail: object shapes, the NOTIFIED_24H gate (§12.5), the taxonomy-bounded
failure surface (§5.4/§5.5), and HMAC webhook signing round-tripping through the
same verify() the ingest stage uses (§9.2)."""

from __future__ import annotations

import pytest

from agent.rails.protocol import Rail
from agent.rails.simulated import SimulatedRail, SimulatedRailError
from agent.rails.types import InvoiceSpec, LinkSpec, MandateSpec, OrderSpec
from agent.rails.webhook_signing import verify

SECRET = "test-webhook-secret"

# `clock` fixture (a controllable FakeClock) comes from tests/agent/conftest.py.


@pytest.fixture
def rail(clock):
    return SimulatedRail(webhook_secret=SECRET, clock=clock)


def test_satisfies_the_rail_protocol_structurally(rail):
    assert isinstance(rail, Rail)
    assert rail.rail_tag == "simulated"


def test_create_order_shape(rail):
    order = rail.create_order(OrderSpec(amount_paise=150_000, receipt="INV-1"))
    assert order.id.startswith("order_")
    assert order.amount_paise == 150_000
    assert order.status == "created"


def test_create_payment_link_and_simulate_payment(rail):
    link = rail.create_payment_link(LinkSpec(amount_paise=50_000, description="Invoice 42"))
    assert link.id.startswith("plink_")
    assert link.status == "created"

    paid = rail.simulate_link_paid(link.id)
    assert paid.status == "paid"
    assert any(w.event_type == "payment_link.paid" for w in rail.emitted_webhooks)


def test_create_invoice_and_simulate_payment(rail):
    invoice = rail.create_invoice(InvoiceSpec(amount_paise=75_000, description="GSTIN corrected"))
    assert invoice.id.startswith("inv_")
    paid = rail.simulate_invoice_paid(invoice.id)
    assert paid.status == "paid"


# ---- Mandate lifecycle + NOTIFIED_24H gate (§12.5) ----


def _make_mandate(rail):
    return rail.create_mandate(
        MandateSpec(max_amount_paise=20_000, start_at="2026-01-01T00:00:00Z", end_at="2027-01-01T00:00:00Z")
    )


def test_present_debit_refused_without_any_predebit_notification(rail):
    mandate = _make_mandate(rail)
    with pytest.raises(SimulatedRailError, match="never sent a pre-debit notification"):
        rail.present_debit(mandate.id, 10_000)


def test_present_debit_refused_before_24h_elapsed(rail, clock):
    mandate = _make_mandate(rail)
    rail.notify_predebit(mandate.id, 10_000, "2026-01-02T00:00:00Z", reason="scheduled installment")
    clock.advance(hours=23, minutes=59)
    with pytest.raises(SimulatedRailError, match="RBI_EMANDATE_PREDEBIT_24H"):
        rail.present_debit(mandate.id, 10_000)


def test_present_debit_succeeds_exactly_at_24h(rail, clock):
    mandate = _make_mandate(rail)
    rail.notify_predebit(mandate.id, 10_000, "2026-01-02T00:00:00Z", reason="scheduled installment")
    clock.advance(hours=24)
    result = rail.present_debit(mandate.id, 10_000)
    assert result.status == "captured"
    assert result.payment_id.startswith("pay_")


def test_present_debit_refused_on_revoked_mandate(rail, clock):
    mandate = _make_mandate(rail)
    rail.notify_predebit(mandate.id, 10_000, "2026-01-02T00:00:00Z", reason="scheduled installment")
    clock.advance(hours=24)
    rail.revoke_mandate(mandate.id)
    with pytest.raises(SimulatedRailError, match="revoked"):
        rail.present_debit(mandate.id, 10_000)


def test_failure_policy_drives_a_taxonomy_backed_failure(clock):
    def always_insufficient_funds(mandate_id: str, amount_paise: int):
        return ("insufficient_funds", "upi")

    failing_rail = SimulatedRail(webhook_secret=SECRET, clock=clock, failure_policy=always_insufficient_funds)
    mandate = _make_mandate(failing_rail)
    failing_rail.notify_predebit(mandate.id, 10_000, "2026-01-02T00:00:00Z", reason="scheduled installment")
    clock.advance(hours=24)

    result = failing_rail.present_debit(mandate.id, 10_000)
    assert result.status == "failed"
    assert result.failure_code == "insufficient_funds"
    assert result.failure_rail == "upi"


def test_failure_policy_cannot_emit_a_code_outside_the_taxonomy(rail, clock):
    def rogue_policy(mandate_id: str, amount_paise: int):
        return ("made_up_reason_nobody_published", "upi")

    mandate = _make_mandate(rail)
    rail.notify_predebit(mandate.id, 10_000, "2026-01-02T00:00:00Z", reason="scheduled installment")
    clock.advance(hours=24)
    rail._failure_policy = rogue_policy  # simulate a misbehaving policy plugged in by a caller

    with pytest.raises(SimulatedRailError, match="outside the taxonomy"):
        rail.present_debit(mandate.id, 10_000)


# ---- Refunds (§11.6) ----


def test_refund_requires_a_captured_payment(rail, clock):
    mandate = _make_mandate(rail)
    rail.notify_predebit(mandate.id, 10_000, "2026-01-02T00:00:00Z", reason="scheduled installment")
    clock.advance(hours=24)
    result = rail.present_debit(mandate.id, 10_000)

    refund = rail.create_refund(result.payment_id, reason="erroneous debit")
    assert refund.status == "processed"
    assert refund.amount_paise == 10_000


def test_refund_rejected_for_unknown_payment(rail):
    with pytest.raises(SimulatedRailError, match="unknown payment_id"):
        rail.create_refund("pay_doesnotexist", reason="test")


# ---- Webhook signing (§9.2) ----


def test_emitted_webhooks_verify_through_the_shared_verify_function(rail):
    link = rail.create_payment_link(LinkSpec(amount_paise=1_000, description="test"))
    rail.simulate_link_paid(link.id)

    assert rail.emitted_webhooks, "expected at least one webhook to have been emitted"
    for webhook in rail.emitted_webhooks:
        assert verify(webhook.body, webhook.signature, SECRET)


def test_webhook_signature_rejected_with_wrong_secret(rail):
    link = rail.create_payment_link(LinkSpec(amount_paise=1_000, description="test"))
    rail.simulate_link_paid(link.id)
    webhook = rail.emitted_webhooks[-1]
    assert not verify(webhook.body, webhook.signature, "wrong-secret")


def test_webhook_signature_rejected_if_body_tampered(rail):
    link = rail.create_payment_link(LinkSpec(amount_paise=1_000, description="test"))
    rail.simulate_link_paid(link.id)
    webhook = rail.emitted_webhooks[-1]
    tampered_body = webhook.body.replace(b'"paid"', b'"cancelled"')
    assert not verify(tampered_body, webhook.signature, SECRET)
