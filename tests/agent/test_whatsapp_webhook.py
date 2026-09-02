"""The inbound WhatsApp webhook -- the half that was missing.

WhatsApp was outbound-only: an approved template went out, the debtor could
reply, and nothing happened. Telegram had had the full loop for two days.
This endpoint closes it by delegating to `handle_inbound_message()`, the
same function the Telegram webhook and the poller already call, so a reply
is diagnosed, decided and answered identically however it arrived.

Most of what is worth testing here is what the endpoint refuses. It is
public, it spends real model calls, and it makes the system speak to a real
person -- so the guards matter more than the happy path.

No real network and no real model: the channel and the extractor are
stand-ins throughout.
"""

from __future__ import annotations

import agent.api.demo as demo_module
import pytest
from fastapi.testclient import TestClient

from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family
from agent.notify.protocol import MessageSendResult
from agent.notify.twilio_signing import expected_signature

AUTH_TOKEN = "an-account-auth-token"
DEMO_PHONE = "+919611550053"
WHATSAPP_FROM = "+19376467656"
URL = "http://testserver/demo/whatsapp-webhook"


class _FakeWhatsApp:
    sent: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    def send(self, *, to, text):
        _FakeWhatsApp.sent.append({"to": to, "text": text})
        return MessageSendResult(channel="whatsapp", external_ref="MM1", status="sent", detail={})

    def close(self):
        pass


def _form(**overrides) -> dict[str, str]:
    form = {
        "MessageSid": "SM1111111111111111111111111111111",
        "AccountSid": "AC00000000000000000000000000000000",
        "From": f"whatsapp:{DEMO_PHONE}",
        "To": f"whatsapp:{WHATSAPP_FROM}",
        "Body": "I can pay 21,000 on the 5th",
        "NumMedia": "0",
        "WaId": "919611550053",
    }
    form.update(overrides)
    return form


def _post(client: TestClient, form: dict[str, str], *, sign_with: str | None = AUTH_TOKEN,
          signature: str | None = None):
    if signature is None and sign_with is not None:
        signature = expected_signature(URL, form, sign_with)
    headers = {"X-Twilio-Signature": signature} if signature is not None else {}
    return client.post("/demo/whatsapp-webhook", data=form, headers=headers)


@pytest.fixture
def client(tmp_path, monkeypatch):
    _FakeWhatsApp.sent.clear()
    monkeypatch.setenv("TRUECOMMIT_EVENTS_DB", str(tmp_path / "events.db"))
    monkeypatch.setenv("TRUECOMMIT_EXTRACTION_LOG", str(tmp_path / "extraction_log.db"))
    monkeypatch.setenv("TRUECOMMIT_CONVERSATION_DB", str(tmp_path / "conversation.db"))
    monkeypatch.setenv("TRUECOMMIT_DEBTORS_DB", str(tmp_path / "debtors.db"))
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC00000000000000000000000000000000")
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", WHATSAPP_FROM)
    monkeypatch.setenv("DEMO_CONTACT_PHONE_NUMBER", DEMO_PHONE)
    monkeypatch.delenv("TWILIO_API_KEY_SECRET", raising=False)
    monkeypatch.delenv("TRUECOMMIT_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(demo_module, "TwilioWhatsAppChannel", _FakeWhatsApp)
    monkeypatch.setattr(demo_module, "compose_reply", lambda reply_text, **kw: "composed reply")
    monkeypatch.setattr(
        demo_module, "extract_from_reply",
        lambda text, **kw: ExtractionResult(family=Family.C, class_=DiagnosisClass.STALLING, confidence=0.5),
    )
    from agent.api.app import app
    with TestClient(app) as c:
        yield c


class TestItActuallyAnswers:
    def test_a_genuine_reply_gets_a_reply(self, client):
        """The whole point: WhatsApp now reads and responds the way Telegram
        does, instead of receiving in silence."""
        response = _post(client, _form())
        assert response.status_code == 200
        assert len(_FakeWhatsApp.sent) == 1
        assert _FakeWhatsApp.sent[0]["to"] == DEMO_PHONE

    def test_it_returns_empty_twiml_rather_than_a_message_body(self, client):
        """The reply already went out over the REST API. A <Message> element
        here would send it a second time."""
        response = _post(client, _form())
        assert response.text.strip() == "<Response/>"
        assert len(_FakeWhatsApp.sent) == 1

    def test_it_strips_the_whatsapp_prefix_before_replying(self, client):
        _post(client, _form())
        assert not _FakeWhatsApp.sent[0]["to"].startswith("whatsapp:")

    def test_a_redelivery_is_not_answered_twice(self, client):
        """Twilio retries. The claim inside handle_inbound_message is keyed
        on MessageSid, so a retry must not produce a second reply to a real
        person."""
        form = _form()
        _post(client, form)
        _post(client, form)
        assert len(_FakeWhatsApp.sent) == 1

    def test_a_genuinely_different_message_is_answered(self, client):
        """The dedupe must not be so broad that it silences real replies."""
        _post(client, _form())
        _post(client, _form(MessageSid="SM2222222222222222222222222222222", Body="ok fine"))
        assert len(_FakeWhatsApp.sent) == 2


class TestWhatItRefuses:
    def test_an_unsigned_request_is_rejected(self, client):
        """The attack this endpoint exists to stop: forging a reply so the
        system answers a message the debtor never sent."""
        response = client.post("/demo/whatsapp-webhook", data=_form())
        assert response.status_code == 403
        assert _FakeWhatsApp.sent == []

    def test_a_forged_signature_is_rejected(self, client):
        response = _post(client, _form(), signature="0/KCTR6DLpKmkAf8muzZqo1nDgQ=")
        assert response.status_code == 403
        assert _FakeWhatsApp.sent == []

    def test_a_body_tampered_after_signing_is_rejected(self, client):
        form = _form()
        signature = expected_signature(URL, form, AUTH_TOKEN)
        response = _post(client, _form(Body="I will pay in full today"), signature=signature)
        assert response.status_code == 403
        assert _FakeWhatsApp.sent == []

    def test_a_signature_from_the_wrong_account_is_rejected(self, client):
        response = _post(client, _form(), sign_with="someone-elses-token")
        assert response.status_code == 403
        assert _FakeWhatsApp.sent == []

    def test_a_stranger_is_ignored_not_answered(self, client):
        """Correctly signed by Twilio, but from someone who is not the demo
        contact. A stranger who finds the number must never be able to drive
        the conversation or spend a model call."""
        form = _form(From="whatsapp:+919999999999", WaId="919999999999")
        response = _post(client, form)
        assert response.status_code == 200
        assert _FakeWhatsApp.sent == []

    def test_it_fails_closed_when_the_demo_contact_is_unset(self, client, monkeypatch):
        """`if configured and phone != configured` would skip the check
        entirely when unset, accepting anyone. That exact fail-open shipped
        once on the Telegram guard (WHAT_BROKE #25); written the right way
        round here from the start."""
        monkeypatch.delenv("DEMO_CONTACT_PHONE_NUMBER", raising=False)
        response = _post(client, _form())
        assert response.status_code == 200
        assert _FakeWhatsApp.sent == []

    def test_it_fails_closed_when_the_auth_token_is_unset(self, client, monkeypatch):
        """With no token there is nothing to verify against, so every
        request must be refused rather than trusted."""
        monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
        response = _post(client, _form())
        assert response.status_code == 200
        assert _FakeWhatsApp.sent == []

    def test_a_message_with_no_text_is_acknowledged_and_ignored(self, client):
        """An image with no caption, a reaction, a status callback. Not a
        debtor reply, and must not be treated as one."""
        response = _post(client, _form(Body="", NumMedia="1"))
        assert response.status_code == 200
        assert _FakeWhatsApp.sent == []

    def test_a_whitespace_only_message_is_ignored(self, client):
        response = _post(client, _form(Body="   "))
        assert response.status_code == 200
        assert _FakeWhatsApp.sent == []


class TestItNeverMakesTwilioRetryForever:
    """Twilio retries a non-2xx. A retry of something that failed for a
    non-transient reason would fail identically forever, so every
    non-authentication refusal returns 200 with empty TwiML."""

    @pytest.mark.parametrize("form", [
        _form(Body=""),
        _form(From="whatsapp:+919999999999"),
        _form(MessageSid=""),
    ])
    def test_non_transient_refusals_return_200(self, client, form):
        assert _post(client, form).status_code == 200

    def test_but_a_bad_signature_still_returns_403(self, client):
        """The one deliberate exception. A forged request is not something
        to acknowledge politely, and a caller who cannot sign will not
        succeed on retry either."""
        assert client.post("/demo/whatsapp-webhook", data=_form()).status_code == 403


class TestThreadIdentity:
    def test_the_conversation_id_is_the_bare_phone_number(self, client):
        """An E.164 number and a Telegram chat id are disjoint address
        spaces, so no `sub:`-style namespacing is needed to keep the threads
        apart. Documented consequence: this is a different debtor record
        from the same person's Telegram thread."""
        captured = {}
        original = demo_module.handle_inbound_message

        def spy(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        client.app.dependency_overrides = {}
        import agent.api.demo as m
        m.handle_inbound_message = spy
        try:
            _post(client, _form())
        finally:
            m.handle_inbound_message = original

        assert captured["conversation_id"] == DEMO_PHONE
        assert captured["channel"] == "whatsapp"
        assert captured["external_id"] == "SM1111111111111111111111111111111"
