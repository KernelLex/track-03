"""The webhook receiver endpoint, exercised with real SimulatedRail webhooks
end to end through actual HTTP request/response (via FastAPI's TestClient,
no real network) -- not just calling verify_and_ingest directly in Python."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent.rails.simulated import SimulatedRail
from agent.rails.types import LinkSpec

SECRET = "api-test-secret"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUECOMMIT_EVENTS_DB", str(tmp_path / "events.db"))
    monkeypatch.setenv("TRUECOMMIT_WEBHOOK_SECRET_SIMULATED", SECRET)

    from agent.api.app import app  # imported after env vars are set, before lifespan runs

    with TestClient(app) as c:
        yield c


def test_health_check():
    from agent.api.app import app

    with TestClient(app) as c:
        response = c.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_valid_webhook_is_ingested_and_facts_are_extracted(client):
    rail = SimulatedRail(webhook_secret=SECRET)
    link = rail.create_payment_link(LinkSpec(amount_paise=25_000, description="api test"))
    rail.simulate_link_paid(link.id)
    webhook = rail.emitted_webhooks[-1]

    response = client.post(
        "/webhooks/simulated", content=webhook.body, headers={"x-razorpay-signature": webhook.signature},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ingested"
    assert "payment_status" in body["facts"]


def test_redelivered_webhook_is_flagged_duplicate_not_reprocessed(client):
    rail = SimulatedRail(webhook_secret=SECRET)
    link = rail.create_payment_link(LinkSpec(amount_paise=10_000, description="api test"))
    rail.simulate_link_paid(link.id)
    webhook = rail.emitted_webhooks[-1]
    headers = {"x-razorpay-signature": webhook.signature}

    first = client.post("/webhooks/simulated", content=webhook.body, headers=headers)
    second = client.post("/webhooks/simulated", content=webhook.body, headers=headers)

    assert first.json()["status"] == "ingested"
    assert second.json()["status"] == "duplicate"


def test_invalid_signature_is_rejected_with_400(client):
    body = json.dumps({"event": "payment.captured", "event_id": "evt_x", "payload": {}}).encode()
    response = client.post("/webhooks/simulated", content=body, headers={"x-razorpay-signature": "0" * 64})
    assert response.status_code == 400


def test_missing_signature_header_is_rejected_with_400(client):
    response = client.post("/webhooks/simulated", content=b"{}")
    assert response.status_code == 400


def test_unconfigured_source_returns_500_rather_than_accepting_unverifiable_input(client):
    response = client.post(
        "/webhooks/some_unconfigured_source", content=b"{}", headers={"x-razorpay-signature": "abc"},
    )
    assert response.status_code == 500


def test_malformed_but_signature_valid_body_is_rejected_with_400(client):
    from agent.rails.webhook_signing import sign

    body = json.dumps({"not_an_envelope": True}).encode()
    response = client.post(
        "/webhooks/simulated", content=body, headers={"x-razorpay-signature": sign(body, SECRET)},
    )
    assert response.status_code == 400


def test_unrecognized_event_type_is_still_ingested_with_200(client):
    from agent.rails.webhook_signing import sign

    body = json.dumps({"event": "something.new", "event_id": "evt_new", "payload": {}}).encode()
    response = client.post(
        "/webhooks/simulated", content=body, headers={"x-razorpay-signature": sign(body, SECRET)},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ingested_unrecognized_event"
