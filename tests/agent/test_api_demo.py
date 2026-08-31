"""The /demo/trigger endpoint -- exercised through real HTTP via FastAPI's
TestClient, with TelegramChannel/TwilioVoiceChannel swapped for fakes so no
real network call happens. The properties under test are the safety ones:
wrong secret refused, recipient always comes from server config never the
request, rate limiting, and check_bounds() actually being consulted.
"""

from __future__ import annotations

import agent.api.demo as demo_module
from agent.notify.protocol import MessageSendResult
from fastapi.testclient import TestClient
import pytest

SECRET = "test-demo-secret"


class _FakeChannel:
    sent: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    def send(self, *, to, text):
        _FakeChannel.sent.append({"to": to, "text": text})
        return MessageSendResult(channel="fake", external_ref="fake-1", status="sent", detail={})

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    demo_module._last_triggered_at.clear()
    _FakeChannel.sent = []
    yield
    demo_module._last_triggered_at.clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUECOMMIT_EVENTS_DB", str(tmp_path / "events.db"))
    monkeypatch.setenv("DEMO_TRIGGER_SECRET", SECRET)
    monkeypatch.setenv("DEMO_CONTACT_TELEGRAM_CHAT_ID", "999888777")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("DEMO_CONTACT_PHONE_NUMBER", "+919999999999")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACfake")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15551234567")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "fake-auth-token")
    monkeypatch.setattr(demo_module, "TelegramChannel", _FakeChannel)
    monkeypatch.setattr(demo_module, "TwilioVoiceChannel", _FakeChannel)

    from agent.api.app import app

    with TestClient(app) as c:
        yield c


def test_wrong_secret_is_refused(client):
    response = client.post("/demo/trigger", json={"secret": "wrong", "channel": "telegram", "scenario": "b2b"})
    assert response.status_code == 403
    assert _FakeChannel.sent == []


def test_missing_secret_env_var_refuses_even_a_blank_secret(client, monkeypatch):
    monkeypatch.delenv("DEMO_TRIGGER_SECRET", raising=False)
    response = client.post("/demo/trigger", json={"secret": "", "channel": "telegram", "scenario": "b2b"})
    assert response.status_code == 403


def test_valid_telegram_trigger_sends_to_the_configured_chat_id(client):
    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "telegram", "scenario": "b2b"})
    assert response.status_code == 200
    assert response.json()["status"] == "sent"
    assert _FakeChannel.sent == [{"to": "999888777", "text": _FakeChannel.sent[0]["text"]}]


def test_valid_ivr_trigger_sends_to_the_configured_phone_number(client):
    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "ivr", "scenario": "subscription"})
    assert response.status_code == 200
    assert _FakeChannel.sent[0]["to"] == "+919999999999"


def test_request_cannot_choose_its_own_recipient(client):
    """The recipient is never taken from the request body -- there's no
    field for it at all, and this asserts the send still goes to the
    server-configured contact even if extra keys are smuggled in."""
    response = client.post(
        "/demo/trigger",
        json={"secret": SECRET, "channel": "telegram", "scenario": "b2b", "to": "+911234567890"},
    )
    assert response.status_code == 200
    assert _FakeChannel.sent[0]["to"] == "999888777"  # the configured chat_id, not the smuggled "to"


def test_unknown_scenario_is_rejected(client):
    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "telegram", "scenario": "nope"})
    assert response.status_code == 400


def test_unknown_channel_is_rejected(client):
    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "sms", "scenario": "b2b"})
    assert response.status_code == 400


def test_second_trigger_within_the_window_is_rate_limited(client):
    first = client.post("/demo/trigger", json={"secret": SECRET, "channel": "telegram", "scenario": "b2b"})
    assert first.status_code == 200
    second = client.post("/demo/trigger", json={"secret": SECRET, "channel": "telegram", "scenario": "b2b"})
    assert second.status_code == 429
    assert len(_FakeChannel.sent) == 1

    third = client.post("/demo/trigger", json={"secret": SECRET, "channel": "ivr", "scenario": "b2b"})
    assert third.status_code == 200  # a different channel isn't rate-limited by the other one's trigger


def test_response_reports_which_bounds_rules_passed(client):
    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "telegram", "scenario": "b2b"})
    assert response.status_code == 200
    assert len(response.json()["bounds_checks"]) > 0


def test_missing_contact_config_returns_a_clear_error_not_a_500(client, monkeypatch):
    monkeypatch.delenv("DEMO_CONTACT_TELEGRAM_CHAT_ID", raising=False)
    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "telegram", "scenario": "b2b"})
    assert response.status_code == 503
