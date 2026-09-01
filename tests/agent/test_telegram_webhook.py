"""The Telegram webhook and the conversation memory behind it.

The failure this exists to prevent is specific and was observed live: the
agent proposed paying the balance on the 19th, the debtor replied "Yes it
works", and the agent answered as though a stranger had said something
vague -- it scored the message STALLING at 0.15 confidence, which is honest
calibration of a message that genuinely is ambiguous *in isolation*. Every
reply was diagnosed standalone, so the system could make an offer and then
fail to recognise the acceptance of it.

No real network and no real model: the channel and both model calls are
stand-ins throughout.
"""

from __future__ import annotations

import agent.api.demo as demo_module
import pytest
from fastapi.testclient import TestClient

from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family
from agent.notify.protocol import MessageSendResult

SECRET = "tg-hook-secret"
CHAT_ID = "999888777"


class _FakeTelegram:
    sent: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    def send(self, *, to, text):
        _FakeTelegram.sent.append({"to": to, "text": text})
        return MessageSendResult(channel="telegram", external_ref="1", status="sent", detail={})

    def close(self):
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUECOMMIT_EVENTS_DB", str(tmp_path / "events.db"))
    monkeypatch.setenv("TRUECOMMIT_EXTRACTION_LOG", str(tmp_path / "extraction_log.db"))
    monkeypatch.setenv("TRUECOMMIT_CONVERSATION_DB", str(tmp_path / "conversation.db"))
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("DEMO_CONTACT_TELEGRAM_CHAT_ID", CHAT_ID)
    monkeypatch.setattr(demo_module, "TelegramChannel", _FakeTelegram)
    monkeypatch.setattr(demo_module, "compose_reply", lambda reply_text, **kw: "composed reply")
    monkeypatch.setattr(
        demo_module, "extract_from_reply",
        lambda text, **kw: ExtractionResult(family=Family.C, class_=DiagnosisClass.STALLING, confidence=0.5),
    )
    _FakeTelegram.sent = []

    from agent.api.app import app

    with TestClient(app) as c:
        yield c


def _update(text: str, update_id: int = 1, chat_id: str = CHAT_ID) -> dict:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def _post(client, update: dict, *, secret: str = SECRET):
    return client.post(
        "/demo/telegram-webhook", json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": secret},
    )


class TestAuthentication:
    def test_a_missing_secret_token_is_refused(self, client):
        response = client.post("/demo/telegram-webhook", json=_update("hello"))
        assert response.status_code == 403
        assert _FakeTelegram.sent == []

    def test_a_wrong_secret_token_is_refused(self, client):
        assert _post(client, _update("hello"), secret="not-it").status_code == 403
        assert _FakeTelegram.sent == []

    def test_a_stranger_cannot_drive_the_conversation(self, client):
        """This endpoint is public. Someone who finds the bot must never
        surface as though they were the demo's own debtor."""
        response = _post(client, _update("hello", chat_id="12345"))
        assert response.status_code == 200
        assert response.json()["reason"] == "not_the_demo_contact"
        assert _FakeTelegram.sent == []


class TestDelivery:
    def test_a_real_reply_is_answered(self, client):
        response = _post(client, _update("I can pay next week"))
        body = response.json()
        assert body["handled"] is True
        assert body["agent_reply"] == "composed reply"
        assert _FakeTelegram.sent == [{"to": CHAT_ID, "text": "composed reply"}]

    def test_a_redelivery_is_not_answered_twice(self, client):
        """Telegram retries. A reply is not free to repeat, and the claim --
        a UNIQUE constraint, not a prior read -- is what stops it."""
        first = _post(client, _update("hello", update_id=77))
        assert first.json()["handled"] is True

        second = _post(client, _update("hello", update_id=77))
        assert second.json()["handled"] is False
        assert second.json()["reason"] == "already_handled"
        assert len(_FakeTelegram.sent) == 1

    def test_a_non_text_update_is_acknowledged_not_treated_as_a_reply(self, client):
        response = _post(client, {"update_id": 5, "message": {"chat": {"id": CHAT_ID}}})
        assert response.status_code == 200
        assert response.json()["handled"] is False
        assert _FakeTelegram.sent == []

    def test_an_unparseable_body_returns_200_rather_than_inviting_retries(self, client):
        """A non-2xx makes Telegram retry, and a body that failed to parse
        once will fail identically forever."""
        response = client.post(
            "/demo/telegram-webhook", content=b"not json",
            headers={"X-Telegram-Bot-Api-Secret-Token": SECRET, "Content-Type": "application/json"},
        )
        assert response.status_code == 200
        assert response.json()["handled"] is False


class TestConversationMemory:
    def test_the_prior_turns_reach_the_extractor(self, client, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            demo_module, "extract_from_reply",
            lambda text, **kw: captured.update(kw) or ExtractionResult(
                family=Family.C, class_=DiagnosisClass.STALLING, confidence=0.5),
        )
        _post(client, _update("first message", update_id=1))
        _post(client, _update("yes that works", update_id=2))

        assert captured["conversation_context"] is not None
        assert "first message" in captured["conversation_context"]
        assert "composed reply" in captured["conversation_context"]

    def test_an_outstanding_plan_is_put_in_front_of_the_composer(self, client, monkeypatch):
        """The fix for the observed failure: "yes it works" is meaningless
        alone and an acceptance against a pending offer."""
        from agent.diagnose.extract import PromiseFields

        captured = {}
        monkeypatch.setattr(demo_module, "compose_reply",
                            lambda reply_text, **kw: captured.update(kw) or "composed reply")
        # First turn states a split, which becomes the outstanding proposal.
        monkeypatch.setattr(
            demo_module, "extract_from_reply",
            lambda text, **kw: ExtractionResult(
                family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.8,
                promise=PromiseFields(amount_paise=21_000_00, date="2026-09-05"),
            ),
        )
        _post(client, _update("I can pay 21,000 on the 5th", update_id=10))
        assert captured.get("outstanding_proposal") is None  # nothing was on the table yet

        # Second turn: a bare acceptance.
        monkeypatch.setattr(
            demo_module, "extract_from_reply",
            lambda text, **kw: ExtractionResult(
                family=Family.C, class_=DiagnosisClass.STALLING, confidence=0.15),
        )
        _post(client, _update("yes it works", update_id=11))

        assert captured["outstanding_proposal"] is not None
        assert "instalment plan" in captured["outstanding_proposal"]

    def test_turns_survive_across_separate_requests(self, client):
        """State is in the store, not in a process variable -- a cold start
        mid-conversation must not lose the thread."""
        _post(client, _update("one", update_id=21))
        _post(client, _update("two", update_id=22))

        store = demo_module._conversation_store()
        try:
            transcript = store.transcript(CHAT_ID)
        finally:
            store.close()
        assert "one" in transcript and "two" in transcript
