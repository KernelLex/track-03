"""Tests for the WhatsApp webhook routes wired into agent/api/app.py:
GET /webhooks/whatsapp (Meta's verification handshake) and
POST /webhooks/whatsapp (real inbound messages). No real Meta account
needed -- signatures are computed the same way agent.notify.whatsapp's own
verify_webhook_signature checks them.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family
from agent.diagnose.llm_extract import ExtractionFailed

APP_SECRET = "meta-app-secret"
VERIFY_TOKEN = "my-verify-token"


def _sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUECOMMIT_EVENTS_DB", str(tmp_path / "events.db"))
    monkeypatch.setenv("TRUECOMMIT_WEBHOOK_SECRET_SIMULATED", "api-test-secret")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", VERIFY_TOKEN)
    monkeypatch.delenv("TRUECOMMIT_LEDGER_DB", raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    from agent.api.app import app

    with TestClient(app) as c:
        yield c


class TestVerificationHandshake:
    def test_matching_token_echoes_challenge_as_plain_text(self, client):
        response = client.get(
            "/webhooks/whatsapp",
            params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "998877"},
        )
        assert response.status_code == 200
        assert response.text == "998877"

    def test_wrong_token_is_refused(self, client):
        response = client.get(
            "/webhooks/whatsapp",
            params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "998877"},
        )
        assert response.status_code == 403

    def test_missing_server_config_is_a_clean_500_not_a_crash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRUECOMMIT_EVENTS_DB", str(tmp_path / "events.db"))
        monkeypatch.setenv("TRUECOMMIT_WEBHOOK_SECRET_SIMULATED", "api-test-secret")
        monkeypatch.delenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", raising=False)
        from agent.api.app import app

        with TestClient(app) as c:
            response = c.get(
                "/webhooks/whatsapp",
                params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1"},
            )
        assert response.status_code == 500


class TestInboundMessages:
    def _post(self, client, payload: dict):
        body = json.dumps(payload).encode()
        return client.post(
            "/webhooks/whatsapp", content=body, headers={"x-hub-signature-256": _sign(body)},
        )

    def test_route_is_not_swallowed_by_the_generic_source_wildcard(self, client):
        """The regression this test guards: /webhooks/{source} was
        registered before /webhooks/whatsapp in an earlier version of this
        file, so a POST here would have 500'd looking for
        TRUECOMMIT_WEBHOOK_SECRET_WHATSAPP and an x-razorpay-signature
        header instead of ever reaching Meta's own verification. Uses a
        button-reply payload (Path A) so this test needs no live
        ANTHROPIC_API_KEY -- free-text extraction is covered separately."""
        payload = {"entry": [{"changes": [{"value": {"messages": [
            {
                "from": "919611550053", "id": "wamid.ROUTE1", "type": "interactive",
                "interactive": {"type": "button_reply", "button_reply": {"id": "btn_already_paid", "title": "I already paid"}},
            },
        ]}}]}]}
        response = self._post(client, payload)
        assert response.status_code == 200
        assert response.json()["status"] == "processed"

    def test_invalid_signature_is_rejected(self, client):
        body = json.dumps({"entry": []}).encode()
        response = client.post(
            "/webhooks/whatsapp", content=body, headers={"x-hub-signature-256": "sha256=deadbeef"},
        )
        assert response.status_code == 400

    def test_missing_signature_header_is_rejected(self, client):
        body = json.dumps({"entry": []}).encode()
        response = client.post("/webhooks/whatsapp", content=body)
        assert response.status_code == 400

    def test_structured_button_reply_diagnosed_via_path_a_no_model(self, client):
        payload = {"entry": [{"changes": [{"value": {"messages": [
            {
                "from": "919611550053", "id": "wamid.BTN1", "type": "interactive",
                "interactive": {"type": "button_reply", "button_reply": {"id": "btn_already_paid", "title": "I already paid"}},
            },
        ]}}]}]}
        response = self._post(client, payload)
        assert response.status_code == 200
        msg = response.json()["messages"][0]
        assert msg["type"] == "interactive"
        assert msg["diagnosis"] == {"family": "B", "class": "ALREADY_PAID_UNRECONCILED", "confidence": 1.0}

    def test_unknown_button_id_yields_no_diagnosis_not_a_crash(self, client):
        payload = {"entry": [{"changes": [{"value": {"messages": [
            {
                "from": "1", "id": "wamid.BTN2", "type": "interactive",
                "interactive": {"type": "button_reply", "button_reply": {"id": "btn_unrecognized", "title": "???"}},
            },
        ]}}]}]}
        response = self._post(client, payload)
        assert response.status_code == 200
        assert response.json()["messages"][0]["diagnosis"] is None

    def test_status_only_delivery_yields_empty_message_list(self, client):
        payload = {"entry": [{"changes": [{"value": {"statuses": [
            {"id": "wamid.OUT1", "status": "delivered"},
        ]}}]}]}
        response = self._post(client, payload)
        assert response.status_code == 200
        assert response.json()["messages"] == []

    def test_free_text_reply_routed_through_path_b_extraction(self, client, monkeypatch):
        """Path B (the real extract_from_reply) is exercised elsewhere against
        a live model (docs/LLM_EXTRACTION.md) -- here it's monkeypatched so
        this test proves the *routing and response shape*, not the model
        call itself, without needing a live ANTHROPIC_API_KEY in CI."""
        import agent.api.app as app_module

        def fake_extract(text: str, *, purpose: str) -> ExtractionResult:
            return ExtractionResult(family=Family.C, **{"class": DiagnosisClass.PROMISE_STATED}, confidence=0.9)

        monkeypatch.setattr(app_module, "extract_from_reply", fake_extract)

        payload = {"entry": [{"changes": [{"value": {"messages": [
            {"from": "919611550053", "id": "wamid.TEXT1", "type": "text", "text": {"body": "will pay Friday"}},
        ]}}]}]}
        response = self._post(client, payload)

        assert response.status_code == 200
        msg = response.json()["messages"][0]
        assert msg["type"] == "text"
        assert msg["diagnosis"] == {"family": "C", "class": "PROMISE_STATED", "confidence": 0.9}

    def test_extraction_failure_is_a_clean_error_field_not_a_500(self, client, monkeypatch):
        import agent.api.app as app_module

        def fake_extract(text: str, *, purpose: str) -> ExtractionResult:
            raise ExtractionFailed("model did not return a parseable ExtractionResult")

        monkeypatch.setattr(app_module, "extract_from_reply", fake_extract)

        payload = {"entry": [{"changes": [{"value": {"messages": [
            {"from": "1", "id": "wamid.TEXT2", "type": "text", "text": {"body": "??"}},
        ]}}]}]}
        response = self._post(client, payload)

        assert response.status_code == 200
        assert "error" in response.json()["messages"][0]["diagnosis"]

    def test_redelivered_message_id_is_deduped_not_rediagnosed(self, client):
        payload = {"entry": [{"changes": [{"value": {"messages": [
            {
                "from": "919611550053", "id": "wamid.DEDUP1", "type": "interactive",
                "interactive": {"type": "button_reply", "button_reply": {"id": "btn_dispute", "title": "I dispute this"}},
            },
        ]}}]}]}
        first = self._post(client, payload)
        second = self._post(client, payload)

        assert first.json()["messages"][0].get("duplicate") is not True
        assert second.json()["messages"][0]["duplicate"] is True
