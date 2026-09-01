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
def _reset_fake_channel():
    """Only this file's own fakes. agent.api.demo's process-global state is
    reset suite-wide by tests/conftest.py instead -- two fixtures resetting
    the same globals is exactly how one of them silently stops matching."""
    _FakeChannel.sent = []
    _FakeChannel.updates = []
    _FakeChannel.last_offset = None
    _FakeChannel.messages = []
    yield


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


def test_a_freshly_booted_machine_does_not_refuse_the_first_trigger(client, monkeypatch):
    """Regression, found by CI on Linux and invisible on a dev box.

    time.monotonic() counts from an arbitrary origin -- machine boot on
    Linux -- so the old `.get(key, 0.0)` default meant "last contacted at
    boot", which on a freshly-started machine reads as *recent*. Every
    first request after a restart was refused with 429 for the length of
    the window, and Render's free tier cold-starts constantly.

    Pinning monotonic() low reproduces a seconds-old machine deterministically
    on any OS, which is the only reason this test is meaningful on the
    Windows box where the bug could never show up naturally.
    """
    monkeypatch.setattr(demo_module.time, "monotonic", lambda: 3.0)

    response = client.post(
        "/demo/trigger", json={"secret": SECRET, "channel": "ivr", "scenario": "b2b", "to": "+919876500011"},
    )
    assert response.status_code == 200, "a just-booted server refused its first trigger"


def test_a_freshly_booted_machine_still_enforces_the_cooldown_after_a_real_send(client, monkeypatch):
    """The fix must not turn the limiter off -- only stop it firing before
    anything has been sent."""
    monkeypatch.setattr(demo_module.time, "monotonic", lambda: 3.0)

    first = client.post(
        "/demo/trigger", json={"secret": SECRET, "channel": "ivr", "scenario": "b2b", "to": "+919876500022"},
    )
    assert first.status_code == 200

    demo_module._last_triggered_at.clear()  # isolate from the per-channel limiter
    second = client.post(
        "/demo/trigger", json={"secret": SECRET, "channel": "ivr", "scenario": "b2b", "to": "+919876500022"},
    )
    assert second.status_code == 429


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


class TestConversationDrivesTheRealDecision:
    """The conversational loop used to diagnose a reply, write a sentence,
    and stop -- so "I'll pay on the 14th" got a polite answer and nothing
    else: no promise recorded, no cooldown, no escalation, and a payment
    link offered to someone who had just asked for time. These cover the
    branch it should have been taking all along.
    """

    def _extraction(self, family, class_, *, promise_date=None):
        from agent.diagnose.extract import ExtractionResult, PromiseFields
        kwargs = {"family": family, "class_": class_, "confidence": 0.8}
        if promise_date:
            kwargs["promise"] = PromiseFields(date=promise_date)
        return ExtractionResult(**kwargs)

    def test_a_dispute_is_routed_to_a_human_not_chased(self, client):
        from agent.diagnose.extract import DiagnosisClass, Family

        decision = demo_module._decide_next_step(
            self._extraction(Family.D, DiagnosisClass.QUANTITY_QUALITY),
            demo_module.SCENARIOS["b2b"], channel="telegram", debtor_key="t_dispute",
        )
        assert decision["action"] == "escalate_human"
        assert decision["debtor_state"] == "DISPUTED_FROZEN"

    def test_a_stated_promise_puts_the_debtor_in_promised_state(self, client):
        """A promise buys quiet time -- PROMISE_COOLDOWN's whole purpose,
        and it can only act if the promise reaches the context."""
        from agent.diagnose.extract import DiagnosisClass, Family

        decision = demo_module._decide_next_step(
            self._extraction(Family.C, DiagnosisClass.PROMISE_STATED, promise_date="2026-09-14"),
            demo_module.SCENARIOS["b2b"], channel="telegram", debtor_key="t_promise",
        )
        assert decision["debtor_state"] == "PROMISED"
        assert decision["promise_date"] == "2026-09-14"

    def test_an_administrative_blocker_reissues_rather_than_asking_for_money(self, client):
        from agent.diagnose.extract import DiagnosisClass, Family

        decision = demo_module._decide_next_step(
            self._extraction(Family.B, DiagnosisClass.PO_MISMATCH),
            demo_module.SCENARIOS["b2b"], channel="telegram", debtor_key="t_blocker",
        )
        assert decision["proposed_action"] == "reissue_artifact"

    def test_chasing_forever_is_not_possible_it_escalates(self, client):
        """ATTEMPT_CEILING stops at six. Past it the answer is a human, not
        silence and not a seventh chase."""
        from agent.diagnose.extract import DiagnosisClass, Family

        extraction = self._extraction(Family.C, DiagnosisClass.STALLING)
        last = None
        for _ in range(8):
            last = demo_module._decide_next_step(
                extraction, demo_module.SCENARIOS["b2b"], channel="telegram", debtor_key="t_ceiling",
            )
        assert last["escalated_to_human"] is True
        assert last["action"] == "escalate_human"
        assert "ATTEMPT_CEILING" in last["refusals"]

    def test_the_composer_is_told_which_action_was_chosen(self, client, monkeypatch):
        from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family

        captured = {}
        monkeypatch.setattr(demo_module, "compose_reply",
                            lambda reply_text, **kw: captured.update(kw) or "ok")
        monkeypatch.setattr(
            demo_module, "extract_from_reply",
            lambda text, **kw: ExtractionResult(
                family=Family.D, class_=DiagnosisClass.QUANTITY_QUALITY, confidence=0.9),
        )
        _FakeChannel.updates = [{"update_id": 900, "message": {"chat": {"id": "999888777"},
                                                              "text": "half the order never arrived"}}]
        client.post("/demo/check-reply", json={"secret": SECRET, "diagnose": True})

        assert captured["next_step"] == "escalate_human"
        # A dispute being escalated must not be handed a payment link.
        assert captured["payment_link"] is None


class TestInstalmentNegotiation:
    """A debtor offering to split the balance is the most common useful
    reply in collections, and the one a dunning bot handles worst -- it
    either ignores the offer and repeats the full amount, or accepts it
    with no instrument behind it."""

    def _reply(self, client, monkeypatch, text, *, amount_paise, promise_date):
        from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family, PromiseFields

        captured = {}
        monkeypatch.setattr(demo_module, "compose_reply",
                            lambda reply_text, **kw: captured.update(kw) or "ok")
        monkeypatch.setattr(
            demo_module, "extract_from_reply",
            lambda t, **kw: ExtractionResult(
                family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.9,
                promise=PromiseFields(amount_paise=amount_paise, date=promise_date),
            ),
        )
        _FakeChannel.updates = [{"update_id": 950, "message": {"chat": {"id": "999888777"}, "text": text}}]
        response = client.post("/demo/check-reply", json={"secret": SECRET, "diagnose": True})
        return response.json(), captured

    def test_a_part_payment_offer_builds_a_real_dated_plan(self, client, monkeypatch):
        body, _ = self._reply(client, monkeypatch, "I can pay 21,000 on the 5th",
                              amount_paise=21_000_00, promise_date="2026-09-05")
        plan = body["payment_plan"]

        assert len(plan["legs"]) == 2
        assert plan["legs"][0]["amount_paise"] == 21_000_00
        assert plan["legs"][0]["due_date"] == "2026-09-05"
        assert plan["legs"][0]["proposed_by"] == "debtor"
        # The balance, and a date the debtor never named.
        assert plan["legs"][1]["amount_paise"] == 42_500_00 - 21_000_00
        assert plan["legs"][1]["proposed_by"] == "system"

    def test_the_plan_carries_a_real_instrument_not_just_a_schedule(self, client, monkeypatch):
        """Two legs over the AFA-free ceiling need authentication per debit
        -- that is the instrument rules' answer, not this module's."""
        body, _ = self._reply(client, monkeypatch, "21,000 on the 5th",
                              amount_paise=21_000_00, promise_date="2026-09-05")
        assert body["payment_plan"]["instrument"].startswith("recurring_emandate")
        assert body["payment_plan"]["requires_afa_per_debit"] is True

    def test_the_plan_is_given_to_the_composer(self, client, monkeypatch):
        _, captured = self._reply(client, monkeypatch, "21,000 on the 5th",
                                  amount_paise=21_000_00, promise_date="2026-09-05")
        assert captured["payment_plan"] is not None
        assert "instalment" in captured["payment_plan"]

    def test_the_split_leg_the_debtor_never_named_is_marked_as_ours(self, client, monkeypatch):
        """Inventing a schedule the debtor never proposed would be putting
        words in their mouth -- so the invented leg says so, and the reply
        can put it as a proposal rather than imply they agreed to it."""
        body, _ = self._reply(client, monkeypatch, "I can pay 21,000 on the 5th",
                              amount_paise=21_000_00, promise_date="2026-09-05")
        proposers = [leg["proposed_by"] for leg in body["payment_plan"]["legs"]]
        assert proposers == ["debtor", "system"]

    def test_paying_the_full_amount_on_a_date_is_a_one_instalment_plan(self, client, monkeypatch):
        """Not a split, but still a dated plan -- which is what earns it a
        real e-mandate link. Nothing is invented: one leg, their amount,
        their date."""
        body, _ = self._reply(client, monkeypatch, "I'll pay the whole thing on the 5th",
                              amount_paise=42_500_00, promise_date="2026-09-05")
        plan = body["payment_plan"]

        assert plan["shape"] == "full"
        assert len(plan["legs"]) == 1
        assert plan["legs"][0]["amount_paise"] == 42_500_00
        assert plan["legs"][0]["due_date"] == "2026-09-05"
        assert plan["legs"][0]["proposed_by"] == "debtor"

    def test_a_date_with_no_amount_is_read_as_the_full_balance(self, client, monkeypatch):
        """Assuming the whole balance is the conservative reading -- it
        never quietly reduces what is owed."""
        body, _ = self._reply(client, monkeypatch, "I'll pay on the 5th",
                              amount_paise=None, promise_date="2026-09-05")
        assert body["payment_plan"]["shape"] == "full"
        assert body["payment_plan"]["legs"][0]["amount_paise"] == 42_500_00

    def test_a_promise_with_no_date_builds_no_plan(self, client, monkeypatch):
        """There is nothing to schedule a debit against."""
        body, _ = self._reply(client, monkeypatch, "I'll pay soon",
                              amount_paise=None, promise_date=None)
        assert "payment_plan" not in body


class TestTheDebtorsOwnScheduleIsHonoured:
    """From a live run. The debtor sent "I can pay 21000 today and rest on
    5 th" -- twice, identically -- and the extractor returned a different
    reading each time:

        run 1: {date: 2026-09-01, amount_paise: 2100000}
        run 2: {date: 2026-09-05, amount_paise: None}

    `PromiseFields` held one (amount, date) pair, so a two-payment offer had
    nowhere to go and got collapsed into one slot, differently on different
    runs. On run 2 the missing amount triggered the "assume the full
    balance" rule, so the system offered Rs 41,650 on the 5th -- and the
    reply contradicted itself, acknowledging the split it had just been told
    about while proposing a plan that wasn't it.
    """

    def _reply(self, client, monkeypatch, text, *, schedule, amount_paise=None, promise_date=None):
        from agent.diagnose.extract import (
            DiagnosisClass, ExtractionResult, Family, PromiseFields, PromiseLeg,
        )

        captured = {}
        monkeypatch.setattr(demo_module, "compose_reply",
                            lambda reply_text, **kw: captured.update(kw) or "ok")
        monkeypatch.setattr(
            demo_module, "extract_from_reply",
            lambda t, **kw: ExtractionResult(
                family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.85,
                promise=PromiseFields(
                    amount_paise=amount_paise, date=promise_date,
                    schedule=[PromiseLeg(**leg) for leg in schedule],
                ),
            ),
        )
        _FakeChannel.updates = [{"update_id": 960, "message": {"chat": {"id": "999888777"}, "text": text}}]
        return client.post("/demo/check-reply", json={"secret": SECRET, "diagnose": True}).json(), captured

    def test_both_dates_they_named_are_used(self, client, monkeypatch):
        body, _ = self._reply(
            client, monkeypatch, "I can pay 21000 today and rest on 5th",
            schedule=[{"amount_paise": 21_000_00, "date": "2026-09-02"},
                      {"amount_paise": None, "date": "2026-09-05"}],
            amount_paise=21_000_00, promise_date="2026-09-02",
        )
        plan = body["payment_plan"]
        assert plan["shape"] == "stated"
        assert [leg["due_date"] for leg in plan["legs"]] == ["2026-09-02", "2026-09-05"]

    def test_the_rest_is_resolved_as_arithmetic_not_invented(self, client, monkeypatch):
        """"the rest" is a real thing people say. The remainder is this
        side's arithmetic -- the model is told not to compute it, because
        inventing an amount the debtor didn't say is what Promise refuses
        everywhere else."""
        body, _ = self._reply(
            client, monkeypatch, "21000 today and rest on 5th",
            schedule=[{"amount_paise": 21_000_00, "date": "2026-09-02"},
                      {"amount_paise": None, "date": "2026-09-05"}],
            amount_paise=21_000_00, promise_date="2026-09-02",
        )
        amounts = [leg["amount_paise"] for leg in body["payment_plan"]["legs"]]
        assert amounts == [21_000_00, 42_500_00 - 21_000_00]

    def test_no_leg_is_marked_as_our_proposal(self, client, monkeypatch):
        """They named the whole schedule, so nothing here is ours to
        propose -- and saying otherwise would understate their commitment."""
        body, _ = self._reply(
            client, monkeypatch, "21000 today and rest on 5th",
            schedule=[{"amount_paise": 21_000_00, "date": "2026-09-02"},
                      {"amount_paise": None, "date": "2026-09-05"}],
            amount_paise=21_000_00, promise_date="2026-09-02",
        )
        assert {leg["proposed_by"] for leg in body["payment_plan"]["legs"]} == {"debtor"}

    def test_a_schedule_that_overshoots_the_invoice_is_refused(self, client, monkeypatch):
        """They are describing a different debt. Repairing it silently would
        misstate what they offered -- the same reason PlanRejected exists."""
        body, _ = self._reply(
            client, monkeypatch, "50000 today and 20000 on the 5th",
            schedule=[{"amount_paise": 50_000_00, "date": "2026-09-02"},
                      {"amount_paise": 20_000_00, "date": "2026-09-05"}],
            amount_paise=50_000_00, promise_date="2026-09-02",
        )
        assert body["payment_plan"]["shape"] != "stated"

    def test_two_unnamed_amounts_are_not_a_schedule(self, client, monkeypatch):
        """"some now and some later" names no amount at all, so there is no
        arithmetic that recovers what they meant."""
        body, _ = self._reply(
            client, monkeypatch, "some now and some later",
            schedule=[{"amount_paise": None, "date": "2026-09-02"},
                      {"amount_paise": None, "date": "2026-09-05"}],
            promise_date="2026-09-02",
        )
        assert body["payment_plan"]["shape"] != "stated"

    def test_a_single_leg_schedule_takes_the_ordinary_path(self, client, monkeypatch):
        """Additive by construction: one payment behaves exactly as before."""
        body, _ = self._reply(
            client, monkeypatch, "I'll pay the whole thing on the 5th",
            schedule=[{"amount_paise": 42_500_00, "date": "2026-09-05"}],
            amount_paise=42_500_00, promise_date="2026-09-05",
        )
        assert body["payment_plan"]["shape"] == "full"
