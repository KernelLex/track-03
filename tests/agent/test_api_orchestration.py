"""Proof of the "nobody clicking" claim: a real payment.failed webhook,
POSTed to the real endpoint, triggers DIAGNOSE -> DECIDE -> BOUNDS -> ACT
on its own -- not a second, parallel test of agent.orchestrate in
isolation (test_orchestrate.py already covers that), but confirmation the
wiring in agent/api/app.py actually calls it."""

from __future__ import annotations

import json

import agent.api.app as app_module
import pytest
from fastapi.testclient import TestClient

from agent.notify.protocol import MessageSendResult
from agent.rails.webhook_signing import sign

SECRET = "api-test-secret"


def _failed_payment_webhook(*, event_id: str, payment_id: str, amount_paise: int, error_code: str) -> tuple[bytes, str]:
    body = json.dumps({
        "event": "payment.failed",
        "event_id": event_id,
        "payload": {"payment": {"entity": {
            "id": payment_id, "amount": amount_paise, "status": "failed", "error_code": error_code,
        }}},
    }).encode()
    return body, sign(body, SECRET)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUECOMMIT_EVENTS_DB", str(tmp_path / "events.db"))
    monkeypatch.setenv("TRUECOMMIT_WEBHOOK_SECRET_SIMULATED", SECRET)
    monkeypatch.setenv("TRUECOMMIT_LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)  # no real send in these tests
    monkeypatch.delenv("TRUECOMMIT_ORCHESTRATOR_RAIL", raising=False)  # SimulatedRail, not real Razorpay

    from agent.api.app import app

    with TestClient(app) as c:
        yield c


def test_a_real_webhook_triggers_the_full_pipeline_with_no_manual_call(client):
    body, signature = _failed_payment_webhook(
        event_id="evt_orch_1", payment_id="pay_orch_1", amount_paise=40_000_00, error_code="insufficient_funds",
    )
    response = client.post("/webhooks/simulated", content=body, headers={"x-razorpay-signature": signature})

    assert response.status_code == 200
    body_json = response.json()
    assert "orchestration" in body_json
    orch = body_json["orchestration"]
    assert orch["diagnosis"] == {"family": "A", "class": "INSUFFICIENT_FUNDS"}
    # Always create_payment_link here, never retry_charge: the webhook path
    # has no real mandate_id to retry against (see app.py's own comment on
    # this), regardless of insufficient_funds being RETRYABLE in the taxonomy.
    assert orch["action_type"] == "create_payment_link"
    assert orch["bounds_passed"] is True


def test_the_action_actually_lands_in_the_real_ledger(client, tmp_path):
    from agent.ledger.store import Ledger

    body, signature = _failed_payment_webhook(
        event_id="evt_orch_2", payment_id="pay_orch_2", amount_paise=15_000_00, error_code="card_expired",
    )
    client.post("/webhooks/simulated", content=body, headers={"x-razorpay-signature": signature})

    with Ledger(str(tmp_path / "ledger.db")) as ledger:
        ledger.verify_chain()  # the chain this webhook wrote into is real and intact
        entries = [e for e in ledger.all_entries() if e.actor == "ORCHESTRATOR"]
        assert len(entries) == 1
        assert entries[0].debtor_id == "debtor_pay_orch_2"


def test_no_orchestration_key_when_ledger_db_is_not_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUECOMMIT_EVENTS_DB", str(tmp_path / "events.db"))
    monkeypatch.setenv("TRUECOMMIT_WEBHOOK_SECRET_SIMULATED", SECRET)
    monkeypatch.delenv("TRUECOMMIT_LEDGER_DB", raising=False)

    from agent.api.app import app

    with TestClient(app) as c:
        body, signature = _failed_payment_webhook(
            event_id="evt_orch_3", payment_id="pay_orch_3", amount_paise=10_000_00, error_code="insufficient_funds",
        )
        response = c.post("/webhooks/simulated", content=body, headers={"x-razorpay-signature": signature})

    assert response.status_code == 200
    assert "orchestration" not in response.json()


def test_a_payment_captured_event_does_not_trigger_orchestration(client):
    """Nothing to diagnose from a success -- Path A only fires on a real
    failure code being present."""
    body = json.dumps({
        "event": "payment.captured", "event_id": "evt_orch_4",
        "payload": {"payment": {"entity": {"id": "pay_orch_4", "status": "captured", "amount": 5_000_00}}},
    }).encode()
    response = client.post(
        "/webhooks/simulated", content=body, headers={"x-razorpay-signature": sign(body, SECRET)},
    )
    assert response.status_code == 200
    assert "orchestration" not in response.json()


def test_an_unmappable_failure_code_is_logged_not_crashed(client):
    """A real code the taxonomy has never heard of must not 500 the webhook
    endpoint -- Razorpay would just retry a 500 forever."""
    body, signature = _failed_payment_webhook(
        event_id="evt_orch_5", payment_id="pay_orch_5", amount_paise=10_000_00, error_code="brand_new_code_not_in_taxonomy",
    )
    response = client.post("/webhooks/simulated", content=body, headers={"x-razorpay-signature": signature})
    assert response.status_code == 200
    assert "orchestration" not in response.json()


class _FakeNotifyChannel:
    sent: list[dict] = []

    def send(self, *, to, text):
        _FakeNotifyChannel.sent.append({"to": to, "text": text})
        return MessageSendResult(channel="fake", external_ref="fake-msg-1", status="sent", detail={})

    def close(self):
        pass


def test_a_created_payment_link_is_actually_sent_to_the_debtor(client):
    """CREATE_PAYMENT_LINK is a rail action, not a MESSAGE_ONLY_ACTION -- ACT
    creating the link isn't the end of the story; the debtor has to actually
    be told about it for the link to recover anything."""
    _FakeNotifyChannel.sent = []
    app_module.app.state.orchestrator_channel = _FakeNotifyChannel()
    app_module.app.state.orchestrator_contact_chat_id = "999888777"

    body, signature = _failed_payment_webhook(
        event_id="evt_orch_notify_1", payment_id="pay_orch_notify_1", amount_paise=25_000_00, error_code="insufficient_funds",
    )
    response = client.post("/webhooks/simulated", content=body, headers={"x-razorpay-signature": signature})

    assert response.status_code == 200
    assert response.json()["orchestration"]["notified"] is True
    assert len(_FakeNotifyChannel.sent) == 1
    assert _FakeNotifyChannel.sent[0]["to"] == "999888777"
    assert "rzp.io" in _FakeNotifyChannel.sent[0]["text"]  # the real short_url, not a placeholder


def test_no_notification_without_a_configured_contact(client):
    app_module.app.state.orchestrator_channel = _FakeNotifyChannel()
    app_module.app.state.orchestrator_contact_chat_id = None
    _FakeNotifyChannel.sent = []

    body, signature = _failed_payment_webhook(
        event_id="evt_orch_notify_2", payment_id="pay_orch_notify_2", amount_paise=25_000_00, error_code="insufficient_funds",
    )
    response = client.post("/webhooks/simulated", content=body, headers={"x-razorpay-signature": signature})

    assert response.json()["orchestration"]["notified"] is False
    assert _FakeNotifyChannel.sent == []


def test_a_redelivered_failed_payment_webhook_does_not_orchestrate_twice(client):
    body, signature = _failed_payment_webhook(
        event_id="evt_orch_6", payment_id="pay_orch_6", amount_paise=20_000_00, error_code="insufficient_funds",
    )
    headers = {"x-razorpay-signature": signature}

    first = client.post("/webhooks/simulated", content=body, headers=headers)
    second = client.post("/webhooks/simulated", content=body, headers=headers)

    assert first.json()["status"] == "ingested"
    assert "orchestration" in first.json()
    assert second.json()["status"] == "duplicate"
    assert "orchestration" not in second.json()
