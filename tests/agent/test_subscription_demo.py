"""The subscription side, finally reachable.

`check_mandate_health()` had existed, been tested, and produced the
Rs 91,72,435 headline in `docs/evidence/AT_RISK_HEADLINE.md` -- and it was
callable from no endpoint. An offline tool ran it once and wrote a markdown
file, and that was the whole demonstration of the project's strongest
claim.

The claim is strong because detection here is *arithmetic on a mandate's own
fields*, not a prediction: `max_amount_paise < upcoming_debit_paise` is a
comparison. These tests exist to keep it honest -- that the endpoint runs
the real detector rather than a demo-shaped copy, that a warning is still a
contact and passes the same gate, and that the rupee figure cannot be
inflated.

No real network: Telegram, Twilio and Razorpay are all absent or stubbed.
"""

from __future__ import annotations

from dataclasses import replace

import agent.api.demo as demo_module
import pytest
from fastapi.testclient import TestClient

from agent.mandate.health import MandateDefect
from agent.mandate.portfolio import (
    FAILURE_KINDS,
    MandatePortfolio,
    PortfolioMandate,
    scan,
    seed_portfolio,
)
from agent.notify.protocol import MessageSendResult

SECRET = "sub-demo-secret"
CHAT_ID = "777000111"


class _FakeTelegram:
    sent: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    def send(self, *, to, text):
        _FakeTelegram.sent.append({"to": to, "text": text})
        return MessageSendResult(channel="telegram", external_ref="s1", status="sent", detail={})

    def close(self):
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUECOMMIT_DEBTORS_DB", str(tmp_path / "debtors.db"))
    monkeypatch.setenv("TRUECOMMIT_CONVERSATION_DB", str(tmp_path / "conversation.db"))
    monkeypatch.setenv("TRUECOMMIT_EVENTS_DB", str(tmp_path / "events.db"))
    monkeypatch.setenv("DEMO_TRIGGER_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_SUBSCRIPTION_BOT_TOKEN", "fake-sub-token")
    monkeypatch.setenv("TELEGRAM_SUBSCRIPTION_WEBHOOK_SECRET", "sub-hook-secret")
    monkeypatch.setenv("DEMO_CONTACT_SUBSCRIPTION_CHAT_ID", CHAT_ID)
    # No rail and no telephony: this suite proves the detection and the
    # gate, not the vendors.
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.setattr(demo_module, "TelegramChannel", _FakeTelegram)
    _FakeTelegram.sent = []

    from agent.api.app import app

    with TestClient(app) as c:
        yield c


def _alert(client, failure="headroom", **extra):
    return client.post("/demo/subscription-alert",
                       json={"secret": SECRET, "failure": failure, **extra})


class TestTheDetectorIsActuallyReachable:
    def test_the_endpoint_runs_the_real_check(self, client):
        """Not a demo-shaped reimplementation. The same
        `check_mandate_health()` the headline evidence and the repair
        lifecycle both call."""
        body = client.get("/demo/mandate-health").json()
        assert body["scanned"] == 8
        assert body["defective"] == 6

    def test_every_defect_type_is_represented(self, client):
        """A demo that always showed a headroom breach would undersell six
        genuinely different failures with six different repairs."""
        counts = client.get("/demo/mandate-health").json()["defect_counts"]
        assert set(counts) == {d.value for d in MandateDefect}

    def test_healthy_mandates_are_reported_healthy(self, client):
        """A detector that flags everything is not a detector. Two of the
        eight are clean and must stay clean."""
        mandates = client.get("/demo/mandate-health").json()["mandates"]
        assert sum(1 for m in mandates if m["healthy"]) == 2

    def test_each_defect_carries_the_arithmetic_that_proves_it(self, client):
        """"This will fail" is worth far more when the reader can check it.
        The detector's own detail string is the proof, and it must survive
        the API boundary."""
        mandates = client.get("/demo/mandate-health").json()["mandates"]
        headroom = next(m for m in mandates
                        if any(d["defect"] == "HEADROOM_BREACH" for d in m["defects"]))
        detail = headroom["defects"][0]["detail"]
        assert "max_amount_paise" in detail and "upcoming_debit_paise" in detail

    def test_the_scan_is_readable_without_the_trigger_secret(self, client):
        assert client.get("/demo/mandate-health").status_code == 200


class TestTheRupeeFigureCannotBeInflated:
    def test_a_mandate_with_two_defects_counts_its_debit_once(self):
        """One debit that will fail is one debit, not two. Summing per
        defect is exactly the kind of number that falls apart the moment
        somebody checks it."""
        both = PortfolioMandate(
            mandate_id="sub_TWO", customer="Double Trouble Ltd", plan="Monthly",
            max_amount_paise=1_000_00,            # headroom breach
            upcoming_debit_paise=50_000_00,       # ... and over the AFA ceiling
            end_at="2027-01-01T00:00:00", next_debit_date="2026-12-01T00:00:00",
            afa_scheduled=False,
        )
        result = scan([both])
        assert result["defective"] == 1
        assert len(result["mandates"][0]["defects"]) >= 2
        assert result["at_risk_paise"] == 50_000_00

    def test_healthy_mandates_contribute_nothing(self):
        healthy = PortfolioMandate(
            mandate_id="sub_OK", customer="Fine Co", plan="Monthly",
            max_amount_paise=50_000_00, upcoming_debit_paise=10_000_00,
            end_at="2027-01-01T00:00:00", next_debit_date="2026-12-01T00:00:00",
            afa_scheduled=True,
        )
        assert scan([healthy])["at_risk_paise"] == 0


class TestTheWarning:
    def test_it_sends_and_names_the_defect(self, client):
        response = _alert(client, "headroom")
        assert response.status_code == 200
        body = response.json()
        assert body["defect"] == "HEADROOM_BREACH"
        assert _FakeTelegram.sent, "a warning that sends nothing is not a warning"

    def test_the_message_says_why_in_the_debtor_s_terms(self, client):
        """The detector's field names are proof, not an explanation. The
        message has to carry both."""
        _alert(client, "headroom")
        text = _FakeTelegram.sent[0]["text"]
        assert "will fail" in text
        assert "ceiling" in text and "21,500" in text

    def test_it_says_nothing_has_been_charged(self, client):
        """The single most reassuring sentence available, and the one that
        distinguishes this from a failed-payment notice."""
        _alert(client, "headroom")
        assert "nothing has been declined" in _FakeTelegram.sent[0]["text"]

    @pytest.mark.parametrize("kind", sorted(FAILURE_KINDS))
    def test_every_failure_button_produces_its_own_defect(self, client, kind):
        response = _alert(client, kind)
        assert response.status_code == 200, response.text
        assert response.json()["defect"]

    def test_an_unknown_failure_kind_is_refused(self, client):
        assert _alert(client, "not-a-real-failure").status_code == 400

    def test_a_healthy_mandate_cannot_be_warned_about(self, client, tmp_path):
        """Once repaired, there is nothing to warn about -- and claiming
        otherwise would be manufacturing an alarm."""
        portfolio = MandatePortfolio(str(tmp_path / "debtors.db"))
        try:
            broken = next(m for m in portfolio.all()
                          if m.mandate_id == FAILURE_KINDS["headroom"])
            portfolio.upsert(replace(
                broken, max_amount_paise=broken.upcoming_debit_paise * 2))
        finally:
            portfolio.close()

        response = _alert(client, "headroom")
        assert response.status_code == 409
        assert "healthy" in response.text

    def test_the_warning_passes_the_same_bounds_gate(self, client):
        """A warning is an outbound contact like any other. Exempting it
        because it is helpful would be exactly the quiet carve-out this
        project exists not to have."""
        body = _alert(client, "headroom").json()
        assert body["bounds_total"] == 20
        assert len(body["bounds_checks"]) == body["bounds_total"]

    def test_the_wrong_secret_is_refused(self, client):
        assert client.post("/demo/subscription-alert",
                           json={"secret": "wrong", "failure": "headroom"}).status_code == 403

    def test_it_lands_on_the_timeline(self, client):
        _alert(client, "expiry")
        events = client.get("/demo/timeline").json()["events"]
        predicted = [e for e in events if e["kind"] == "failure_predicted"]
        assert predicted and predicted[0]["detail"]["defect"] == "EXPIRY_BEFORE_DEBIT"


class TestTheSecondBotIsSeparate:
    def test_its_webhook_refuses_the_wrong_secret(self, client):
        response = client.post(
            "/demo/telegram-webhook/subscription",
            json={"update_id": 1, "message": {"chat": {"id": CHAT_ID}, "text": "hi"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "not-it"},
        )
        assert response.status_code == 403

    def test_a_stranger_cannot_drive_the_subscription_conversation(self, client):
        response = client.post(
            "/demo/telegram-webhook/subscription",
            json={"update_id": 2, "message": {"chat": {"id": "9999"}, "text": "hi"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "sub-hook-secret"},
        )
        assert response.status_code == 200
        assert response.json()["reason"] == "not_the_demo_contact"

    def test_the_two_bots_have_different_secrets(self, client, monkeypatch):
        """The b2b bot's secret must not open the subscription webhook. Two
        bots sharing a secret would make a 403 ambiguous about which one
        broke, which is the entire reason they are separate routes."""
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "b2b-secret")
        response = client.post(
            "/demo/telegram-webhook/subscription",
            json={"update_id": 3, "message": {"chat": {"id": CHAT_ID}, "text": "hi"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "b2b-secret"},
        )
        assert response.status_code == 403


class TestTheThreadsDoNotCollide:
    """Telegram's private-chat id is the *user's* id, not a per-bot one, so
    both bots report the same chat id for the same person.

    Keying on that alone would have merged the two conversations: one
    transcript, one outstanding proposal, and a plan offered by one bot
    acceptable to the other. Two bots that look separate in Telegram and are
    a single conversation underneath is worse than not splitting them.
    """

    def test_the_subscription_thread_is_namespaced(self, client):
        from agent.api.demo import SUBSCRIPTION_THREAD_PREFIX, _subscription_conversation_id

        assert _subscription_conversation_id(CHAT_ID) != CHAT_ID
        assert _subscription_conversation_id(CHAT_ID).startswith(SUBSCRIPTION_THREAD_PREFIX)

    def test_an_alert_records_against_the_namespaced_thread(self, client):
        _alert(client, "headroom")
        events = client.get("/demo/timeline").json()["events"]
        predicted = [e for e in events if e["kind"] == "failure_predicted"]
        assert predicted[0]["conversation_id"].startswith("sub:")

    def test_the_b2b_thread_is_untouched_by_a_subscription_alert(self, client):
        """The specific failure this guards: same person, same chat id, two
        bots -- and the b2b case file must not fill up with subscription
        events."""
        _alert(client, "headroom")
        b2b = client.get("/demo/timeline", params={"conversation_id": CHAT_ID}).json()
        assert b2b["events"] == []


class TestTheContactGuardFailsClosed:
    """`if configured and chat_id != configured` skipped the check entirely
    when the variable was unset, so an unconfigured deployment accepted a
    message from *any* chat, ran a real model call on it, and replied -- on
    a public endpoint.

    Caught in production by a probe from chat id "1" coming back
    `handled: true` instead of `not_the_demo_contact`. The b2b webhook had
    the identical bug and was safe only because its variable happened to be
    set.
    """

    def test_an_unconfigured_subscription_contact_refuses_everyone(self, client, monkeypatch):
        monkeypatch.delenv("DEMO_CONTACT_SUBSCRIPTION_CHAT_ID", raising=False)
        response = client.post(
            "/demo/telegram-webhook/subscription",
            json={"update_id": 41, "message": {"chat": {"id": "1"}, "text": "probe"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "sub-hook-secret"},
        )
        assert response.json()["handled"] is False
        assert response.json()["reason"] == "demo_contact_not_configured"

    def test_an_unconfigured_b2b_contact_refuses_everyone(self, client, monkeypatch):
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "b2b-secret")
        monkeypatch.delenv("DEMO_CONTACT_TELEGRAM_CHAT_ID", raising=False)
        response = client.post(
            "/demo/telegram-webhook",
            json={"update_id": 42, "message": {"chat": {"id": "1"}, "text": "probe"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "b2b-secret"},
        )
        assert response.json()["reason"] == "demo_contact_not_configured"

    def test_the_configured_contact_still_gets_through(self, client, monkeypatch):
        """Failing closed must not close on the person it exists for.

        Asserted by whether the extractor was *reached*, not by the reply --
        a guard that rejects at the door never gets that far, and that is
        the difference this test is measuring."""
        from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family

        reached = []
        monkeypatch.setattr(
            demo_module, "extract_from_reply",
            lambda t, **kw: reached.append(t) or ExtractionResult(
                family=Family.C, class_=DiagnosisClass.STALLING, confidence=0.5),
        )
        monkeypatch.setattr(demo_module, "compose_reply", lambda reply_text, **kw: "ok")

        client.post(
            "/demo/telegram-webhook/subscription",
            json={"update_id": 43, "message": {"chat": {"id": CHAT_ID}, "text": "hi"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "sub-hook-secret"},
        )
        assert reached == ["hi"], "the configured contact was refused at the door"
