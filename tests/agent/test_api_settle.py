"""SETTLE against the live webhook path (§16, Law 7).

The half of the pipeline that had never run against a real payment.
DIAGNOSE->DECIDE->BOUNDS->ACT was wired to a live webhook long before this
was; a real `payment.captured` arriving used to be ingested, turned into
facts, and then dropped, because the orchestrator only looked for a failure
code. That gap is why docs/RESULTS.md could only describe recovery as "a
modelling convention for my harness" -- the number came from the harness,
never from the rail.

These tests hold the line on the three properties that make attribution
mean anything: only 'captured' counts, it counts exactly once, and it
carries the rail that produced it.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

SECRET = "settle-test-secret"


def _signed(body_dict: dict) -> tuple[bytes, str]:
    body = json.dumps(body_dict).encode("utf-8")
    signature = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return body, signature


def _captured_payment(payment_id="pay_real1", amount=42_500_00, **entity_extra) -> dict:
    """A real Razorpay `payment.captured` envelope shape: no top-level
    event_id (that arrives as the x-razorpay-event-id header), the payment
    entity nested under payload.payment.entity."""
    entity = {"id": payment_id, "status": "captured", "amount": amount}
    entity.update(entity_extra)
    return {"entity": "event", "event": "payment.captured", "contains": ["payment"],
            "payload": {"payment": {"entity": entity}}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUECOMMIT_EVENTS_DB", str(tmp_path / "events.db"))
    monkeypatch.setenv("TRUECOMMIT_LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("TRUECOMMIT_WEBHOOK_SECRET_RAZORPAY", SECRET)

    from agent.api.app import app

    with TestClient(app) as c:
        yield c


def _post(client, body_dict, event_id):
    body, signature = _signed(body_dict)
    return client.post(
        "/webhooks/razorpay", content=body,
        headers={"x-razorpay-signature": signature, "x-razorpay-event-id": event_id},
    )


def test_a_captured_payment_is_attributed_as_recovered(client):
    response = _post(client, _captured_payment(), "evt_settle_1")
    assert response.status_code == 200
    settlement = response.json()["settlement"]

    assert settlement["attributed"] is True
    assert settlement["payment_id"] == "pay_real1"
    assert settlement["amount_paise"] == 42_500_00
    assert settlement["rail_tag"] == "razorpay"  # Law 6 -- never "simulated"


def test_the_same_payment_is_never_counted_twice(client):
    """A redelivery that somehow passed INGEST's own dedup still can't
    double-count: UNIQUE(payment_id) in the database decides, not code."""
    first = _post(client, _captured_payment(payment_id="pay_dup"), "evt_settle_2a")
    assert first.json()["settlement"]["attributed"] is True

    # Same payment, a different event id -- so INGEST sees a genuinely new
    # delivery and SETTLE is the only thing standing between it and a
    # double-count.
    second = _post(client, _captured_payment(payment_id="pay_dup"), "evt_settle_2b")
    settlement = second.json()["settlement"]
    assert settlement["attributed"] is False
    assert settlement["reason"] == "already_attributed"


def test_a_failed_payment_is_never_attributed(client):
    """§16: not authorized, not created -- only a rail-confirmed capture."""
    body = _captured_payment(payment_id="pay_failed")
    body["event"] = "payment.failed"
    body["payload"]["payment"]["entity"]["status"] = "failed"
    body["payload"]["payment"]["entity"]["error_code"] = "insufficient_funds"

    response = _post(client, body, "evt_settle_3")
    assert response.status_code == 200
    assert "settlement" not in response.json()


def test_an_authorized_but_uncaptured_payment_is_not_recovery(client):
    body = _captured_payment(payment_id="pay_authorized")
    body["payload"]["payment"]["entity"]["status"] = "authorized"

    response = _post(client, body, "evt_settle_4")
    assert response.status_code == 200
    assert "settlement" not in response.json()


def test_the_invoice_the_payment_actually_paid_is_used(client):
    """A payment against an invoice carries that invoice's id -- attribute
    to it rather than to a placeholder derived from the payment."""
    response = _post(client, _captured_payment(payment_id="pay_inv", invoice_id="inv_REAL123"), "evt_settle_5")
    assert response.json()["settlement"]["invoice_id"] == "inv_REAL123"


def test_merchant_notes_supply_the_real_ids_when_present(client):
    """Razorpay's arbitrary merchant metadata is where a real deployment
    puts its own AR system's ids -- the documented answer to the derived
    placeholder ids this build otherwise falls back to."""
    response = _post(
        client,
        _captured_payment(
            payment_id="pay_notes",
            notes={"debtor_id": "debtor_acme", "invoice_id": "INV-2201"},
        ),
        "evt_settle_6",
    )
    settlement = response.json()["settlement"]
    assert settlement["debtor_id"] == "debtor_acme"
    assert settlement["invoice_id"] == "INV-2201"


def test_without_a_ledger_configured_nothing_is_attributed(tmp_path, monkeypatch):
    """Same policy the orchestrator already has: no ledger, no silent
    in-memory stand-in that would report recovery nobody can audit."""
    monkeypatch.setenv("TRUECOMMIT_EVENTS_DB", str(tmp_path / "events.db"))
    monkeypatch.setenv("TRUECOMMIT_WEBHOOK_SECRET_RAZORPAY", SECRET)
    monkeypatch.delenv("TRUECOMMIT_LEDGER_DB", raising=False)

    from agent.api.app import app

    with TestClient(app) as c:
        response = _post(c, _captured_payment(payment_id="pay_noledger"), "evt_settle_7")
    assert response.status_code == 200
    assert "settlement" not in response.json()
