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

    def __init__(self, *args, **kwargs):
        pass

    def send(self, *, to, text):
        _FakeChannel.sent.append({"to": to, "text": text})
        return MessageSendResult(channel="fake", external_ref="fake-1", status="sent", detail={})

    def get_updates(self, *, offset=None):
        _FakeChannel.last_offset = offset
        return _FakeChannel.updates

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    demo_module._last_triggered_at.clear()
    _FakeChannel.sent = []
    _FakeChannel.updates = []
    _FakeChannel.last_offset = None
    yield
    demo_module._last_triggered_at.clear()


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


class _FakeRazorpayRail:
    """Stand-in for RazorpayRail -- asserts the real create_payment_link()
    call shape without ever making a real Razorpay API call."""
    created_specs: list = []
    raises: bool = False

    def __init__(self, *, key_id, key_secret):
        pass

    def create_payment_link(self, spec):
        if _FakeRazorpayRail.raises:
            raise RuntimeError("razorpay unavailable")
        _FakeRazorpayRail.created_specs.append(spec)
        from agent.rails.types import PaymentLink
        return PaymentLink(id="plink_fake1", short_url="https://rzp.io/i/fake123", amount_paise=spec.amount_paise, status="created")


@pytest.fixture(autouse=True)
def _reset_razorpay_fake():
    _FakeRazorpayRail.created_specs = []
    _FakeRazorpayRail.raises = False
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


def test_b2b_trigger_still_sends_if_link_creation_raises(client, monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_fake")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "fake_secret")
    monkeypatch.setattr(demo_module, "RazorpayRail", _FakeRazorpayRail)
    _FakeRazorpayRail.raises = True

    response = client.post("/demo/trigger", json={"secret": SECRET, "channel": "telegram", "scenario": "b2b"})
    assert response.status_code == 200
    assert "rzp.io" not in _FakeChannel.sent[0]["text"]


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
        assert "review" in body["agent_reply"].lower()
        # The follow-up is a second, real send -- not just text echoed in the response.
        followup_sends = [s for s in _FakeChannel.sent if s["text"] == body["agent_reply"]]
        assert len(followup_sends) == 1
        assert followup_sends[0]["to"] == "999888777"

    def test_family_c_followup_resends_the_real_link_when_one_exists(self, client, monkeypatch):
        from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family

        demo_module._last_payment_link_url = "https://rzp.io/i/fromEarlierRun"
        fake_result = ExtractionResult(family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.7)
        monkeypatch.setattr(demo_module, "extract_from_reply", lambda text, **kw: fake_result)

        _FakeChannel.updates = [self._update(101, "999888777", "send me the link again")]
        response = client.post("/demo/check-reply", json={"secret": SECRET, "diagnose": True})
        assert "https://rzp.io/i/fromEarlierRun" in response.json()["agent_reply"]

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
