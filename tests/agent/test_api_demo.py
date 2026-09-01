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
    updates: list[dict] = []
    last_offset = None
    messages: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    def send(self, *, to, text):
        _FakeChannel.sent.append({"to": to, "text": text})
        return MessageSendResult(channel="fake", external_ref="fake-1", status="sent", detail={})

    def send_template(self, *, to, content_sid, content_variables):
        _FakeChannel.sent.append({"to": to, "content_sid": content_sid, "content_variables": content_variables})
        return MessageSendResult(channel="fake", external_ref="fake-template-1", status="sent", detail={})

    def get_updates(self, *, offset=None):
        _FakeChannel.last_offset = offset
        return _FakeChannel.updates

    def list_messages(self, *, to, from_, limit=5):
        return _FakeChannel.messages

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    demo_module._last_triggered_at.clear()
    demo_module._last_triggered_at_by_number.clear()
    demo_module._last_followed_up_update_id = 0
    demo_module._last_followed_up_whatsapp_sid = None
    _FakeChannel.sent = []
    _FakeChannel.updates = []
    _FakeChannel.last_offset = None
    _FakeChannel.messages = []
    yield
    demo_module._last_triggered_at.clear()
    demo_module._last_triggered_at_by_number.clear()
    demo_module._last_followed_up_update_id = 0
    demo_module._last_followed_up_whatsapp_sid = None


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUECOMMIT_EVENTS_DB", str(tmp_path / "events.db"))
    monkeypatch.setenv("TRUECOMMIT_EXTRACTION_LOG", str(tmp_path / "extraction_log.db"))
    monkeypatch.setenv("DEMO_TRIGGER_SECRET", SECRET)
    monkeypatch.setenv("DEMO_CONTACT_TELEGRAM_CHAT_ID", "999888777")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("DEMO_CONTACT_PHONE_NUMBER", "+919999999999")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACfake")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15551234567")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "fake-auth-token")
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", "whatsapp:+19376467656")
    monkeypatch.setenv("TWILIO_WHATSAPP_CONTENT_SID", "HXfaketemplate")
    monkeypatch.setattr(demo_module, "TelegramChannel", _FakeChannel)
    monkeypatch.setattr(demo_module, "TwilioVoiceChannel", _FakeChannel)
    monkeypatch.setattr(demo_module, "TwilioWhatsAppChannel", _FakeChannel)
    # Explicit, so no test ever depends on a real Anthropic call -- or on
    # the composer happening to fail for want of an API key, which is what
    # would otherwise silently exercise the fallback path everywhere.
    monkeypatch.setattr(
        demo_module, "compose_reply",
        lambda reply_text, **kw: f"composed: {reply_text[:40]}",
    )

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


def test_whatsapp_trigger_sends_a_template_with_the_real_link_as_a_variable(client, monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_fake")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "fake_secret")
    monkeypatch.setattr(demo_module, "RazorpayRail", _FakeRazorpayRail)

    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "whatsapp", "scenario": "b2b"})
    assert response.status_code == 200
    sent = _FakeChannel.sent[0]
    assert sent["to"] == "+919999999999"
    assert sent["content_sid"] == "HXfaketemplate"
    assert sent["content_variables"]["1"] == "INV-2201"
    assert sent["content_variables"]["4"] == "https://rzp.io/i/fake123"


def test_whatsapp_trigger_only_supports_the_b2b_scenario(client):
    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "whatsapp", "scenario": "subscription"})
    assert response.status_code == 400
    assert _FakeChannel.sent == []


def test_whatsapp_trigger_falls_back_to_the_projects_own_template(client, monkeypatch):
    """The ContentSid is a resource id, not a secret -- an unset env var
    uses this project's own real template rather than failing, so it isn't
    one more thing to configure on every deployment."""
    monkeypatch.delenv("TWILIO_WHATSAPP_CONTENT_SID", raising=False)
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_fake")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "fake_secret")
    monkeypatch.setattr(demo_module, "RazorpayRail", _FakeRazorpayRail)
    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "whatsapp", "scenario": "b2b"})
    assert response.status_code == 200
    assert _FakeChannel.sent[0]["content_sid"] == demo_module.DEFAULT_WHATSAPP_CONTENT_SID


def test_whatsapp_refuses_to_send_a_placeholder_link(client, monkeypatch):
    """A WhatsApp template variable can't be empty, and inventing a URL to
    fill it is worse than not sending -- so with no real payable URL
    available the send is refused outright."""
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "whatsapp", "scenario": "b2b"})
    assert response.status_code == 503
    assert _FakeChannel.sent == []


def test_whatsapp_trigger_missing_twilio_config_returns_a_clear_error(client, monkeypatch):
    monkeypatch.delenv("TWILIO_WHATSAPP_FROM", raising=False)
    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "whatsapp", "scenario": "b2b"})
    assert response.status_code == 503


class _FakeRazorpayRail:
    """Stand-in for RazorpayRail -- asserts the real create_payment_link()
    call shape without ever making a real Razorpay API call."""
    created_specs: list = []
    raises: bool = False

    def __init__(self, *, key_id, key_secret):
        pass

    created_invoice_specs: list = []
    invoice_raises: bool = False

    def create_payment_link(self, spec):
        if _FakeRazorpayRail.raises:
            raise RuntimeError("test mode limit of 30 reached for payment_link")
        _FakeRazorpayRail.created_specs.append(spec)
        from agent.rails.types import PaymentLink
        return PaymentLink(id="plink_fake1", short_url="https://rzp.io/i/fake123", amount_paise=spec.amount_paise, status="created")

    def create_invoice(self, spec):
        if _FakeRazorpayRail.invoice_raises:
            raise RuntimeError("invoices unavailable too")
        _FakeRazorpayRail.created_invoice_specs.append(spec)
        from agent.rails.types import Invoice
        return Invoice(id="inv_fake1", short_url="https://rzp.io/rzp/fakeInv", amount_paise=spec.amount_paise, status="issued")


@pytest.fixture(autouse=True)
def _reset_razorpay_fake():
    _FakeRazorpayRail.created_specs = []
    _FakeRazorpayRail.created_invoice_specs = []
    _FakeRazorpayRail.raises = False
    _FakeRazorpayRail.invoice_raises = False
    demo_module._last_payment_link_url = None
    yield


def test_b2b_trigger_includes_a_real_payment_link_when_razorpay_is_configured(client, monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_fake")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "fake_secret")
    monkeypatch.setattr(demo_module, "RazorpayRail", _FakeRazorpayRail)

    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "telegram", "scenario": "b2b"})
    assert response.status_code == 200
    assert "https://rzp.io/i/fake123" in _FakeChannel.sent[0]["text"]
    assert len(_FakeRazorpayRail.created_specs) == 1
    assert _FakeRazorpayRail.created_specs[0].amount_paise == 42_500_00


def test_b2b_trigger_still_sends_if_razorpay_credentials_are_missing(client, monkeypatch):
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)

    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "telegram", "scenario": "b2b"})
    assert response.status_code == 200
    assert "rzp.io" not in _FakeChannel.sent[0]["text"]


def test_second_b2b_trigger_reuses_the_first_links_url_not_a_fresh_one(client, monkeypatch):
    """Razorpay test-mode caps payment links at 30 total -- creating a new
    one per click burns through that fast. A second trigger should reuse
    the first run's link, not call create_payment_link() again."""
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_fake")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "fake_secret")
    monkeypatch.setattr(demo_module, "RazorpayRail", _FakeRazorpayRail)

    demo_module._last_triggered_at.clear()
    client.post("/demo/trigger", json={"secret": SECRET, "channel": "telegram", "scenario": "b2b"})
    demo_module._last_triggered_at.clear()
    client.post("/demo/trigger", json={"secret": SECRET, "channel": "telegram", "scenario": "b2b"})

    assert len(_FakeRazorpayRail.created_specs) == 1
    assert _FakeChannel.sent[0]["text"] == _FakeChannel.sent[1]["text"]


def test_a_capped_payment_link_falls_back_to_a_real_invoice(client, monkeypatch):
    """Live-caught: Razorpay's 30-payment-link test-mode cap counts lifetime
    creates, not live links -- cancelling old ones frees nothing, so on an
    exhausted account create_payment_link can never succeed again. A real
    invoice is the fallback: also rail-created, also payable, and arguably
    the better fit since the scenario is an overdue invoice."""
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_fake")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "fake_secret")
    monkeypatch.setattr(demo_module, "RazorpayRail", _FakeRazorpayRail)
    _FakeRazorpayRail.raises = True  # links exhausted

    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "telegram", "scenario": "b2b"})
    assert response.status_code == 200
    assert len(_FakeRazorpayRail.created_invoice_specs) == 1
    assert "https://rzp.io/rzp/fakeInv" in _FakeChannel.sent[0]["text"]


def test_no_payable_url_at_all_still_lets_telegram_send_without_one(client, monkeypatch):
    """Telegram's body is free-form, so it degrades by omitting the line --
    unlike the WhatsApp template, whose variable can't be empty."""
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_fake")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "fake_secret")
    monkeypatch.setattr(demo_module, "RazorpayRail", _FakeRazorpayRail)
    _FakeRazorpayRail.raises = True
    _FakeRazorpayRail.invoice_raises = True

    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "telegram", "scenario": "b2b"})
    assert response.status_code == 200
    assert "Pay now" not in _FakeChannel.sent[0]["text"]


def test_subscription_scenario_never_gets_a_payment_link(client, monkeypatch):
    """Only b2b's message references a link at all -- the other scenarios
    shouldn't attempt real Razorpay calls they have no use for."""
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_fake")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "fake_secret")
    monkeypatch.setattr(demo_module, "RazorpayRail", _FakeRazorpayRail)

    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "telegram", "scenario": "subscription"})
    assert response.status_code == 200
    assert _FakeRazorpayRail.created_specs == []


def test_escalation_scenario_sends_escalation_specific_text_not_the_b2b_message(client):
    """Regression test: the escalation scenario's live trigger initially had
    no server-side text of its own and silently fell back to the b2b
    message, which describes a completely different invoice. Caught before
    publishing the dashboard, not after."""
    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "telegram", "scenario": "escalation"})
    assert response.status_code == 200
    sent_text = _FakeChannel.sent[0]["text"]
    assert "INV-5581" in sent_text
    assert "INV-2201" not in sent_text


def test_telegram_rejects_a_caller_supplied_number_rather_than_ignoring_it(client):
    """Telegram can't message a cold number regardless -- rejected loudly,
    not silently ignored, since staying quiet would look like a bug rather
    than the platform rule it is."""
    response = client.post(
        "/demo/trigger",
        json={"secret": SECRET, "channel": "telegram", "scenario": "b2b", "to": "+911234567890"},
    )
    assert response.status_code == 400
    assert _FakeChannel.sent == []


def test_telegram_without_a_number_still_goes_to_the_configured_chat_id(client):
    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "telegram", "scenario": "b2b"})
    assert response.status_code == 200
    assert _FakeChannel.sent[0]["to"] == "999888777"


def test_ivr_can_call_a_caller_supplied_number(client):
    response = client.post(
        "/demo/trigger",
        json={"secret": SECRET, "channel": "ivr", "scenario": "b2b", "to": "+911234567890"},
    )
    assert response.status_code == 200
    assert _FakeChannel.sent[0]["to"] == "+911234567890"


def test_whatsapp_can_message_a_caller_supplied_number(client, monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_fake")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "fake_secret")
    monkeypatch.setattr(demo_module, "RazorpayRail", _FakeRazorpayRail)
    response = client.post(
        "/demo/trigger",
        json={"secret": SECRET, "channel": "whatsapp", "scenario": "b2b", "to": "+911234567890"},
    )
    assert response.status_code == 200
    assert _FakeChannel.sent[0]["to"] == "+911234567890"


def test_a_caller_supplied_number_must_be_e164(client):
    response = client.post(
        "/demo/trigger",
        json={"secret": SECRET, "channel": "ivr", "scenario": "b2b", "to": "9611550053"},
    )
    assert response.status_code == 400
    assert _FakeChannel.sent == []


def test_the_same_supplied_number_has_its_own_cooldown(client):
    first = client.post(
        "/demo/trigger", json={"secret": SECRET, "channel": "ivr", "scenario": "b2b", "to": "+911234567890"},
    )
    assert first.status_code == 200

    # A different number is unaffected -- this is per-number, not a blanket
    # "one custom contact per window".
    demo_module._last_triggered_at.clear()
    other = client.post(
        "/demo/trigger", json={"secret": SECRET, "channel": "ivr", "scenario": "b2b", "to": "+911234567891"},
    )
    assert other.status_code == 200

    demo_module._last_triggered_at.clear()
    same_again = client.post(
        "/demo/trigger", json={"secret": SECRET, "channel": "ivr", "scenario": "b2b", "to": "+911234567890"},
    )
    assert same_again.status_code == 429


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


def test_a_clean_channel_level_failure_still_returns_200_with_detail(client, monkeypatch):
    """Regression test for a real gap found live: a channel-level failure
    (e.g. Twilio's API answering with status=failed rather than raising)
    used to come back as HTTP 200 with no way to tell it wasn't actually
    sent, or why. The endpoint stays 200 (the request itself succeeded --
    check_bounds ran, the channel was reached) but detail must be present."""
    class _FailingChannel(_FakeChannel):
        def send(self, *, to, text):
            return MessageSendResult(
                channel="fake", external_ref=None, status="failed",
                detail={"status_code": 400, "message": "trial accounts have limited parameter access"},
            )

    monkeypatch.setattr(demo_module, "TelegramChannel", _FailingChannel)
    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "telegram", "scenario": "b2b"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert "trial accounts" in body["detail"]["message"]


def test_missing_contact_config_returns_a_clear_error_not_a_500(client, monkeypatch):
    monkeypatch.delenv("DEMO_CONTACT_TELEGRAM_CHAT_ID", raising=False)
    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "telegram", "scenario": "b2b"})
    assert response.status_code == 503


class TestCheckReply:
    """/demo/check-reply -- the reactive piece: did the demo owner reply on
    Telegram yet, and if so, what does the real extractor make of it."""

    def _update(self, update_id, chat_id, text):
        return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}

    def test_no_updates_means_no_reply(self, client):
        _FakeChannel.updates = []
        response = client.post("/demo/check-reply", json={"secret": SECRET, "diagnose": False})
        assert response.status_code == 200
        assert response.json() == {"has_reply": False}

    def test_a_new_message_from_the_configured_chat_is_surfaced(self, client):
        _FakeChannel.updates = [self._update(101, "999888777", "we'll pay next week")]
        response = client.post("/demo/check-reply", json={"secret": SECRET, "diagnose": False})
        body = response.json()
        assert body["has_reply"] is True
        assert body["text"] == "we'll pay next week"
        assert body["update_id"] == 101

    def test_a_message_from_a_different_chat_is_never_surfaced(self, client):
        """A stranger messaging the bot during a live demo must never be
        mistaken for the demo's own configured debtor."""
        _FakeChannel.updates = [self._update(101, "someone-elses-chat-id", "hello?")]
        response = client.post("/demo/check-reply", json={"secret": SECRET, "diagnose": False})
        assert response.json() == {"has_reply": False}

    def test_after_update_id_is_translated_to_telegrams_offset_semantics(self, client):
        client.post("/demo/check-reply", json={"secret": SECRET, "after_update_id": 55, "diagnose": False})
        assert _FakeChannel.last_offset == 56  # Telegram's offset excludes the given id; +1 gets "strictly after"

    def test_no_after_update_id_means_no_offset_sent(self, client):
        client.post("/demo/check-reply", json={"secret": SECRET, "diagnose": False})
        assert _FakeChannel.last_offset is None

    def test_diagnose_true_runs_the_real_extractor_and_returns_its_result(self, client, monkeypatch):
        from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family

        fake_result = ExtractionResult(family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.9)
        monkeypatch.setattr(demo_module, "extract_from_reply", lambda text, **kw: fake_result)

        _FakeChannel.updates = [self._update(101, "999888777", "we'll pay next week")]
        response = client.post("/demo/check-reply", json={"secret": SECRET, "diagnose": True})
        body = response.json()
        assert body["diagnosis"] == {"family": "C", "class": "PROMISE_STATED", "confidence": 0.9}

    def test_a_diagnosed_reply_gets_a_real_followup_sent_back(self, client, monkeypatch):
        """The conversational half: check-reply doesn't just diagnose and
        stop -- it sends a real message back over the same channel, and
        reports what it sent."""
        from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family

        fake_result = ExtractionResult(family=Family.D, class_=DiagnosisClass.QUANTITY_QUALITY, confidence=0.9)
        monkeypatch.setattr(demo_module, "extract_from_reply", lambda text, **kw: fake_result)

        _FakeChannel.updates = [self._update(101, "999888777", "this is disputed")]
        response = client.post("/demo/check-reply", json={"secret": SECRET, "diagnose": True})
        body = response.json()

        assert body["agent_reply"] is not None
        # The follow-up is a second, real send -- not just text echoed in the response.
        followup_sends = [s for s in _FakeChannel.sent if s["text"] == body["agent_reply"]]
        assert len(followup_sends) == 1
        assert followup_sends[0]["to"] == "999888777"

    def test_the_followup_is_composed_from_the_debtors_actual_words(self, client, monkeypatch):
        """Not a fixed line per family: the composer gets the real message,
        the real diagnosis, and the real invoice context."""
        from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family

        captured = {}

        def _fake_compose(reply_text, **kwargs):
            captured["reply_text"] = reply_text
            captured.update(kwargs)
            return "a specific, composed reply"

        monkeypatch.setattr(demo_module, "compose_reply", _fake_compose)
        fake_result = ExtractionResult(family=Family.D, class_=DiagnosisClass.QUANTITY_QUALITY, confidence=0.9)
        monkeypatch.setattr(demo_module, "extract_from_reply", lambda text, **kw: fake_result)

        _FakeChannel.updates = [self._update(101, "999888777", "half the order never arrived")]
        response = client.post("/demo/check-reply", json={"secret": SECRET, "diagnose": True})

        assert response.json()["agent_reply"] == "a specific, composed reply"
        assert captured["reply_text"] == "half the order never arrived"
        assert captured["family"] == "D"
        assert captured["class_"] == "QUANTITY_QUALITY"
        assert captured["invoice_id"] == "INV-2201"
        assert captured["days_overdue"] == 22

    def test_a_failed_composer_falls_back_to_the_fixed_line_rather_than_going_silent(self, client, monkeypatch):
        from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family
        from agent.notify.compose import ComposeFailed

        def _boom(reply_text, **kwargs):
            raise ComposeFailed("no budget left")

        monkeypatch.setattr(demo_module, "compose_reply", _boom)
        fake_result = ExtractionResult(family=Family.D, class_=DiagnosisClass.QUANTITY_QUALITY, confidence=0.9)
        monkeypatch.setattr(demo_module, "extract_from_reply", lambda text, **kw: fake_result)

        _FakeChannel.updates = [self._update(101, "999888777", "this is disputed")]
        response = client.post("/demo/check-reply", json={"secret": SECRET, "diagnose": True})

        assert "review" in response.json()["agent_reply"].lower()  # the known-safe Family D line

    def test_querying_the_same_reply_twice_only_sends_the_followup_once(self, client, monkeypatch):
        """Live-caught: diagnosis is harmless to repeat, but a real send
        isn't -- a page reload or a repeated poll asking about the same
        update_id must not trigger a second real Telegram message."""
        from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family

        fake_result = ExtractionResult(family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.8)
        monkeypatch.setattr(demo_module, "extract_from_reply", lambda text, **kw: fake_result)
        _FakeChannel.updates = [self._update(101, "999888777", "will pay soon")]

        first = client.post("/demo/check-reply", json={"secret": SECRET, "diagnose": True})
        assert first.json()["agent_reply"] is not None

        second = client.post("/demo/check-reply", json={"secret": SECRET, "diagnose": True})
        assert second.json()["agent_reply"] is None  # diagnosis still runs, the send is skipped
        assert second.json()["diagnosis"] is not None

        followup_sends = [s for s in _FakeChannel.sent if s["to"] == "999888777"]
        assert len(followup_sends) == 1

    def test_the_real_payment_link_is_given_to_the_composer_as_context(self, client, monkeypatch):
        """The composer decides whether to include the link (only if they
        actually asked for a way to pay) -- but it can't include one it was
        never given, so passing it through is what's asserted here."""
        from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family

        captured = {}
        monkeypatch.setattr(
            demo_module, "compose_reply",
            lambda reply_text, **kw: captured.update(kw) or "here you go",
        )
        demo_module._last_payment_link_url = "https://rzp.io/i/fromEarlierRun"
        fake_result = ExtractionResult(family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.7)
        monkeypatch.setattr(demo_module, "extract_from_reply", lambda text, **kw: fake_result)

        _FakeChannel.updates = [self._update(101, "999888777", "send me the link again")]
        client.post("/demo/check-reply", json={"secret": SECRET, "diagnose": True})
        assert captured["payment_link"] == "https://rzp.io/i/fromEarlierRun"

    def test_diagnose_false_skips_the_extractor_entirely(self, client, monkeypatch):
        called = []
        monkeypatch.setattr(demo_module, "extract_from_reply", lambda text, **kw: called.append(text))

        _FakeChannel.updates = [self._update(101, "999888777", "we'll pay next week")]
        client.post("/demo/check-reply", json={"secret": SECRET, "diagnose": False})
        assert called == []

    def test_extraction_failure_is_reported_not_a_500(self, client, monkeypatch):
        from agent.diagnose.llm_extract import ExtractionFailed

        def _raise(text, **kw):
            raise ExtractionFailed("no budget left")
        monkeypatch.setattr(demo_module, "extract_from_reply", _raise)

        _FakeChannel.updates = [self._update(101, "999888777", "we'll pay next week")]
        response = client.post("/demo/check-reply", json={"secret": SECRET, "diagnose": True})
        assert response.status_code == 200
        assert "no budget left" in response.json()["diagnosis"]["error"]

    def test_wrong_secret_is_refused(self, client):
        response = client.post("/demo/check-reply", json={"secret": "wrong"})
        assert response.status_code == 403

    def test_missing_contact_config_returns_a_clear_error(self, client, monkeypatch):
        monkeypatch.delenv("DEMO_CONTACT_TELEGRAM_CHAT_ID", raising=False)
        response = client.post("/demo/check-reply", json={"secret": SECRET})
        assert response.status_code == 503


class TestCheckWhatsAppReply:
    """/demo/check-reply?channel=whatsapp -- polls this account's own
    Twilio message history rather than a live webhook (see
    _check_whatsapp_reply's docstring)."""

    def _msg(self, sid, body):
        return {"sid": sid, "body": body, "direction": "inbound"}

    def test_no_messages_means_no_reply(self, client):
        _FakeChannel.messages = []
        response = client.post("/demo/check-reply", json={"secret": SECRET, "channel": "whatsapp", "diagnose": False})
        assert response.json() == {"has_reply": False}

    def test_a_new_message_is_surfaced(self, client):
        _FakeChannel.messages = [self._msg("SM2", "will pay by friday")]
        response = client.post("/demo/check-reply", json={"secret": SECRET, "channel": "whatsapp", "diagnose": False})
        body = response.json()
        assert body["has_reply"] is True
        assert body["text"] == "will pay by friday"
        assert body["update_id"] == "SM2"

    def test_same_message_sid_again_is_not_a_new_reply(self, client):
        _FakeChannel.messages = [self._msg("SM2", "will pay by friday")]
        response = client.post(
            "/demo/check-reply",
            json={"secret": SECRET, "channel": "whatsapp", "after_message_sid": "SM2", "diagnose": False},
        )
        assert response.json() == {"has_reply": False}

    def test_diagnosed_reply_gets_a_real_whatsapp_followup(self, client, monkeypatch):
        from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family

        fake_result = ExtractionResult(family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.7)
        monkeypatch.setattr(demo_module, "extract_from_reply", lambda text, **kw: fake_result)
        _FakeChannel.messages = [self._msg("SM3", "will pay soon")]

        response = client.post("/demo/check-reply", json={"secret": SECRET, "channel": "whatsapp", "diagnose": True})
        body = response.json()
        assert body["diagnosis"]["family"] == "C"
        assert body["agent_reply"] is not None

        followup_sends = [s for s in _FakeChannel.sent if s.get("to") == "+919999999999"]
        assert len(followup_sends) == 1
        assert followup_sends[0]["text"] == body["agent_reply"]

    def test_querying_the_same_whatsapp_reply_twice_only_sends_the_followup_once(self, client, monkeypatch):
        from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family

        fake_result = ExtractionResult(family=Family.A, class_=DiagnosisClass.INSTRUMENT_EXPIRED, confidence=0.8)
        monkeypatch.setattr(demo_module, "extract_from_reply", lambda text, **kw: fake_result)
        _FakeChannel.messages = [self._msg("SM4", "card expired")]

        first = client.post("/demo/check-reply", json={"secret": SECRET, "channel": "whatsapp", "diagnose": True})
        assert first.json()["agent_reply"] is not None
        second = client.post("/demo/check-reply", json={"secret": SECRET, "channel": "whatsapp", "diagnose": True})
        assert second.json()["agent_reply"] is None

        followup_sends = [s for s in _FakeChannel.sent if s.get("to") == "+919999999999"]
        assert len(followup_sends) == 1

    def test_whatsapp_missing_contact_config_returns_a_clear_error(self, client, monkeypatch):
        monkeypatch.delenv("DEMO_CONTACT_PHONE_NUMBER", raising=False)
        response = client.post("/demo/check-reply", json={"secret": SECRET, "channel": "whatsapp"})
        assert response.status_code == 503
