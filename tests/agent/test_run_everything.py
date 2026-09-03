"""Driving every channel at once, and the allowlist that lets a second
person receive it.

Two things are being protected here. One is convenience: three buttons in
the right order while talking to an audience is easy to get wrong. The
other is the guard those buttons sit behind -- opening the Telegram webhook
to a judge's own chat must not open it to everyone, and `docs/WHAT_BROKE.md`
#25 is what happened the last time that guard failed open.
"""

from __future__ import annotations

import agent.api.demo as demo_module
import pytest
from fastapi.testclient import TestClient

from agent.api.demo_allowlist import TelegramAllowlist
from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family
from agent.notify.protocol import MessageSendResult

SECRET = "run-all-secret"
OWNER_CHAT = "8327566456"
JUDGE_CHAT = "5551234567"


class _FakeChannel:
    sent: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    def send(self, *, to, text):
        _FakeChannel.sent.append({"to": to, "text": text})
        return MessageSendResult(channel="fake", external_ref="X1", status="sent", detail={})

    def send_template(self, *, to, content_sid, content_variables):
        _FakeChannel.sent.append({"to": to, "template": content_sid})
        return MessageSendResult(channel="whatsapp", external_ref="MM1", status="sent", detail={})

    def place_call(self, *, to, text):
        _FakeChannel.sent.append({"to": to, "call": True})
        return MessageSendResult(channel="ivr", external_ref="CA1", status="sent", detail={})

    def close(self):
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    _FakeChannel.sent.clear()
    for var, value in (
        ("TRUECOMMIT_EVENTS_DB", str(tmp_path / "events.db")),
        ("TRUECOMMIT_CONVERSATION_DB", str(tmp_path / "conv.db")),
        ("TRUECOMMIT_DEBTORS_DB", str(tmp_path / "debtors.db")),
        ("TRUECOMMIT_ALLOWLIST_DB", str(tmp_path / "allow.db")),
        ("DEMO_TRIGGER_SECRET", SECRET),
        ("TELEGRAM_WEBHOOK_SECRET", "tg-secret"),
        ("TELEGRAM_BOT_TOKEN", "fake"),
        ("DEMO_CONTACT_TELEGRAM_CHAT_ID", OWNER_CHAT),
    ):
        monkeypatch.setenv(var, value)
    monkeypatch.setattr(demo_module, "TelegramChannel", _FakeChannel)
    monkeypatch.setattr(demo_module, "compose_reply", lambda reply_text, **kw: "composed")
    monkeypatch.setattr(
        demo_module, "extract_from_reply",
        lambda text, **kw: ExtractionResult(family=Family.C, class_=DiagnosisClass.STALLING, confidence=0.5))
    from agent.api.app import app
    with TestClient(app) as c:
        yield c


def _tg_update(chat_id, text="I can pay on the 5th"):
    return {"update_id": abs(hash(text + str(chat_id))) % 10**8,
            "message": {"text": text, "chat": {"id": int(chat_id)}}}


class TestTheWebhookStaysFailClosed:
    def test_a_stranger_is_still_ignored(self, client):
        """The property that must survive this feature."""
        res = client.post("/demo/telegram-webhook", json=_tg_update("777000111"),
                          headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"})
        assert res.json()["handled"] is False
        assert _FakeChannel.sent == []

    def test_an_allowlisted_chat_is_answered(self, client, tmp_path):
        """The point of the feature: a judge who opted in gets a reply."""
        with TelegramAllowlist(str(tmp_path / "allow.db")) as a:
            a.allow(JUDGE_CHAT)
        res = client.post("/demo/telegram-webhook", json=_tg_update(JUDGE_CHAT),
                          headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"})
        assert res.json().get("handled") is not False
        assert _FakeChannel.sent, "the judge should have received a reply"

    def test_the_reply_goes_to_the_chat_that_wrote(self, client, tmp_path):
        """Not to the demo owner. Sending a judge's answer to someone else
        would be worse than not answering."""
        with TelegramAllowlist(str(tmp_path / "allow.db")) as a:
            a.allow(JUDGE_CHAT)
        client.post("/demo/telegram-webhook", json=_tg_update(JUDGE_CHAT),
                    headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"})
        assert _FakeChannel.sent[0]["to"] == JUDGE_CHAT

    def test_a_revoked_chat_stops_being_answered(self, client, tmp_path):
        with TelegramAllowlist(str(tmp_path / "allow.db")) as a:
            a.allow(JUDGE_CHAT)
            a.revoke(JUDGE_CHAT)
        res = client.post("/demo/telegram-webhook", json=_tg_update(JUDGE_CHAT),
                          headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"})
        assert res.json()["handled"] is False

    def test_an_unset_contact_with_an_empty_allowlist_accepts_nobody(self, client, monkeypatch):
        """WHAT_BROKE #25 in one assertion."""
        monkeypatch.delenv("DEMO_CONTACT_TELEGRAM_CHAT_ID", raising=False)
        res = client.post("/demo/telegram-webhook", json=_tg_update("123123123"),
                          headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"})
        assert res.json()["handled"] is False
        assert _FakeChannel.sent == []


class TestTelegramChatIdValidation:
    def test_a_phone_number_in_the_telegram_field_is_refused_with_a_reason(self, client):
        """Silently failing at Telegram's API would leave the user with no
        idea which field was wrong."""
        res = client.post("/demo/trigger", json={
            "secret": SECRET, "channel": "telegram", "scenario": "b2b", "to": "+919611550053"})
        assert res.status_code == 400
        assert "chat id" in res.json()["detail"].lower()

    def test_a_numeric_chat_id_is_accepted(self, client):
        res = client.post("/demo/trigger", json={
            "secret": SECRET, "channel": "telegram", "scenario": "b2b", "to": JUDGE_CHAT})
        assert res.status_code == 200
        assert _FakeChannel.sent[0]["to"] == JUDGE_CHAT

    def test_sending_to_a_chat_id_allowlists_it_for_replies(self, client, tmp_path):
        """Otherwise the judge receives a message and is then ignored, which
        is worse than not offering the field."""
        client.post("/demo/trigger", json={
            "secret": SECRET, "channel": "telegram", "scenario": "b2b", "to": JUDGE_CHAT})
        with TelegramAllowlist(str(tmp_path / "allow.db")) as a:
            assert a.is_allowed(JUDGE_CHAT)


class TestRunEverything:
    def test_it_requires_the_secret(self, client):
        res = client.post("/demo/run-everything", json={"secret": "wrong"})
        assert res.status_code == 403

    def test_one_channel_failing_does_not_stop_the_others(self, client, monkeypatch):
        """The behaviour that makes this usable mid-demo. Twilio is not
        configured in this fixture, so WhatsApp and the call fail -- Telegram
        must still go."""
        res = client.post("/demo/run-everything", json={"secret": SECRET})
        assert res.status_code == 200
        body = res.json()
        assert "telegram" in body["succeeded"]
        assert body["failed"], "whatsapp and ivr should have failed without Twilio config"
        assert _FakeChannel.sent, "telegram should still have been sent"

    def test_every_channel_is_reported_separately(self, client):
        body = client.post("/demo/run-everything", json={"secret": SECRET}).json()
        for channel in ("telegram", "whatsapp", "ivr"):
            assert channel in body["results"]

    def test_telegram_is_skipped_rather_than_misdirected_when_unaddressable(self, client, monkeypatch):
        """With no chat id and no configured contact there is nobody to
        message. Falling back to the server's own chat would send a judge's
        demo to the demo owner instead."""
        monkeypatch.delenv("DEMO_CONTACT_TELEGRAM_CHAT_ID", raising=False)
        body = client.post("/demo/run-everything", json={"secret": SECRET}).json()
        assert "skipped" in body["results"]["telegram"]
        assert "telegram" not in body["attempted"]

    def test_an_unknown_scenario_is_refused(self, client):
        res = client.post("/demo/run-everything", json={"secret": SECRET, "scenario": "nope"})
        assert res.status_code == 404
