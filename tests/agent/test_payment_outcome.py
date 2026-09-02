"""Telling the debtor what happened to their payment, and letting a real
capture move their score.

The gap this closes: the entire conversation was about getting someone to
pay, and the moment they did, the system went silent. Worse for a failure
-- the debtor believes they have paid and will not act again until told
otherwise.

No real network: the channel and the rail are both stand-ins.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import agent.api.app as app_module
import pytest
from fastapi.testclient import TestClient

from agent.debtor.registry import Debtor, DebtorRegistry
from agent.notify.protocol import MessageSendResult

SECRET = "sim-secret"
CHAT_ID = "555000111"


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
    monkeypatch.setenv("TRUECOMMIT_LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("TRUECOMMIT_CONVERSATION_DB", str(tmp_path / "conversation.db"))
    monkeypatch.setenv("TRUECOMMIT_DEBTORS_DB", str(tmp_path / "debtors.db"))
    monkeypatch.setenv("TRUECOMMIT_WEBHOOK_SECRET_SIMULATED", SECRET)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("DEMO_CONTACT_TELEGRAM_CHAT_ID", CHAT_ID)
    monkeypatch.setattr(app_module, "TelegramChannel", _FakeTelegram)
    _FakeTelegram.sent = []

    from agent.api.app import app

    with TestClient(app) as c:
        yield c


def _post(client, event: str, payload: dict, *, event_id: str):
    body = json.dumps(payload).encode()
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/simulated", content=body,
        headers={"Content-Type": "application/json",
                 "X-Razorpay-Signature": sig, "X-Razorpay-Event-Id": event_id},
    )


def _captured(payment_id="pay_ok_1", amount=42_500_00):
    return {"event": "payment.captured",
            "payload": {"payment": {"entity": {"id": payment_id, "amount": amount,
                                               "status": "captured"}}}}


def _failed(payment_id="pay_bad_1", code="BAD_ACCOUNT"):
    return {"event": "payment.failed",
            "payload": {"payment": {"entity": {"id": payment_id, "amount": 42_500_00,
                                               "status": "failed", "error_code": code}}}}


class TestTheDebtorIsTold:
    def test_a_capture_is_reported_as_received(self, client):
        _post(client, "payment.captured", _captured(), event_id="evt_1")
        assert len(_FakeTelegram.sent) == 1
        text = _FakeTelegram.sent[0]["text"]
        assert "Payment received" in text
        assert "pay_ok_1" in text

    def test_a_capture_says_chasing_has_stopped(self, client):
        """The single most useful sentence in the message: a debtor who has
        paid should not have to wonder whether they will be chased again."""
        _post(client, "payment.captured", _captured(), event_id="evt_1")
        assert "stopped" in _FakeTelegram.sent[0]["text"]

    def test_a_failure_is_reported_and_says_no_money_moved(self, client):
        """The debtor believes they have paid. Left alone, they will not act
        again -- and they must not think a retry took their money."""
        _post(client, "payment.failed", _failed(), event_id="evt_2")
        text = _FakeTelegram.sent[0]["text"]
        assert "didn't go through" in text
        assert "Nothing has been taken" in text

    def test_a_failure_message_makes_no_demand(self, client):
        """DIAGNOSE -> DECIDE -> BOUNDS -> ACT is already running on this
        same webhook and owns what happens next. This message exists only so
        the debtor isn't left believing a failure succeeded."""
        _post(client, "payment.failed", _failed(), event_id="evt_2")
        text = _FakeTelegram.sent[0]["text"].lower()
        assert "pay now" not in text
        assert "overdue" not in text

    def test_an_unrelated_event_tells_the_debtor_nothing(self, client):
        _post(client, "payment.authorized",
              {"event": "payment.authorized",
               "payload": {"payment": {"entity": {"id": "pay_x", "amount": 100,
                                                  "status": "authorized"}}}},
              event_id="evt_3")
        assert _FakeTelegram.sent == []

    def test_the_outcome_lands_on_the_timeline(self, client):
        _post(client, "payment.captured", _captured(), event_id="evt_1")
        body = client.get("/demo/timeline").json()
        assert any(e["kind"] == "payment_captured" for e in body["events"])


class TestACaptureMovesTheScore:
    # The app seeds `debtor_live` against DEMO_CONTACT_TELEGRAM_CHAT_ID at
    # startup, so that is the debtor a capture on this chat belongs to.
    # Creating a second one here would collide on channel_ref -- which is
    # exactly the misrouting the unique index now refuses.
    DEBTOR_ID = "debtor_live"

    def _register(self, client, tmp_path):
        return DebtorRegistry(str(tmp_path / "debtors.db"))

    def test_a_real_capture_keeps_an_open_promise(self, client, tmp_path):
        registry = self._register(client, tmp_path)
        registry.record_promise(self.DEBTOR_ID, invoice_id="INV-2201",
                                amount_paise=42_500_00, promised_date="2026-09-05")
        registry.close()

        _post(client, "payment.captured", _captured(), event_id="evt_1")

        registry = DebtorRegistry(str(tmp_path / "debtors.db"))
        try:
            outcomes = registry.outcomes_for(self.DEBTOR_ID)
            assert [o.outcome for o in outcomes] == ["kept"]
            assert outcomes[0].payment_id == "pay_ok_1"
        finally:
            registry.close()

    def test_a_failed_payment_does_not_keep_a_promise(self, client, tmp_path):
        """Only a rail-confirmed capture counts -- Law 7's standard, the
        same one RecoveryLedger.attribute() enforces."""
        registry = self._register(client, tmp_path)
        registry.record_promise(self.DEBTOR_ID, invoice_id="INV-2201",
                                amount_paise=42_500_00, promised_date="2026-09-05")
        registry.close()

        _post(client, "payment.failed", _failed(), event_id="evt_2")

        registry = DebtorRegistry(str(tmp_path / "debtors.db"))
        try:
            assert [o.outcome for o in registry.outcomes_for(self.DEBTOR_ID)] == ["pending"]
        finally:
            registry.close()

    def test_a_capture_with_no_open_promise_is_still_reported(self, client, tmp_path):
        """Nothing to settle is not a reason to go quiet about a payment."""
        registry = self._register(client, tmp_path)
        registry.close()
        _post(client, "payment.captured", _captured(), event_id="evt_1")
        assert len(_FakeTelegram.sent) == 1


class TestTheAdminView:
    def test_the_register_lists_debtors_with_their_terms(self, client):
        body = client.get("/demo/debtors").json()
        assert body["debtors"], "seeding should have run at startup"
        row = body["debtors"][0]
        assert {"band", "credibility_pct", "rationale"} <= set(row["score"])
        assert {"grace_days", "max_instalments"} <= set(row["terms"])

    def test_every_row_says_whether_its_history_is_seeded(self, client):
        """The distinction a viewer must never have to guess at."""
        for row in client.get("/demo/debtors").json()["debtors"]:
            assert isinstance(row["is_seeded"], bool)

    def test_the_published_bands_come_back_with_the_list(self, client):
        bands = client.get("/demo/debtors").json()["bands"]
        assert [b["band"] for b in bands] == ["trusted", "standard", "watch", "strict"]

    def test_a_debtor_detail_carries_the_promises_behind_the_score(self, client):
        body = client.get("/demo/debtors/debtor_orbit").json()
        assert body["score"]["band"] == "strict"
        assert len(body["promises"]) == 5
        assert {"turns", "events"} <= set(body)

    def test_an_unknown_debtor_is_a_404_not_an_empty_record(self, client):
        assert client.get("/demo/debtors/nobody").status_code == 404


class TestScoringDoesNotDependOnMessaging:
    """Whether a payment counts toward a debtor's record is a fact about
    the payment. Getting this backwards -- settling *inside* the notifier --
    meant a deployment with no Telegram token silently stopped scoring
    altogether, which is invisible until someone asks why a score is wrong.
    """

    @pytest.fixture
    def client_without_a_channel(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRUECOMMIT_EVENTS_DB", str(tmp_path / "events.db"))
        monkeypatch.setenv("TRUECOMMIT_LEDGER_DB", str(tmp_path / "ledger.db"))
        monkeypatch.setenv("TRUECOMMIT_CONVERSATION_DB", str(tmp_path / "conversation.db"))
        monkeypatch.setenv("TRUECOMMIT_DEBTORS_DB", str(tmp_path / "debtors.db"))
        monkeypatch.setenv("TRUECOMMIT_WEBHOOK_SECRET_SIMULATED", SECRET)
        monkeypatch.setenv("DEMO_CONTACT_TELEGRAM_CHAT_ID", CHAT_ID)
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)

        from agent.api.app import app

        with TestClient(app) as c:
            yield c

    def test_a_capture_still_settles_with_no_channel_configured(
        self, client_without_a_channel, tmp_path,
    ):
        registry = DebtorRegistry(str(tmp_path / "debtors.db"))
        registry.record_promise("debtor_live", invoice_id="INV-2201",
                                amount_paise=42_500_00, promised_date="2026-09-05")
        registry.close()

        response = _post(client_without_a_channel, "payment.captured",
                         _captured(), event_id="evt_nochannel")
        assert response.json()["debtor_notified"]["promise_settled"] is True

        registry = DebtorRegistry(str(tmp_path / "debtors.db"))
        try:
            assert [o.outcome for o in registry.outcomes_for("debtor_live")] == ["kept"]
        finally:
            registry.close()

    def test_it_reports_honestly_that_nobody_was_told(self, client_without_a_channel):
        """Silently claiming the debtor was notified would be worse than the
        bug -- the operator needs to know the message didn't go out."""
        body = _post(client_without_a_channel, "payment.captured",
                     _captured(), event_id="evt_nochannel").json()
        assert body["debtor_notified"]["notified"] is False
        assert body["debtor_notified"]["reason"] == "no_channel_configured"

    def test_the_timeline_records_it_even_though_no_message_went_out(
        self, client_without_a_channel,
    ):
        _post(client_without_a_channel, "payment.captured", _captured(), event_id="evt_nochannel")
        events = client_without_a_channel.get("/demo/timeline").json()["events"]
        captured = [e for e in events if e["kind"] == "payment_captured"]
        assert captured and captured[0]["detail"]["notified"] is False


class TestBothBugsFoundInProduction:
    """Two defects a real Rs 42,500 capture exposed on the live service.

    Neither showed in any test, because both tests and fixtures used the
    same invoice id on both sides and never delivered the same payment
    twice -- which is exactly what a real rail does.
    """

    def test_a_capture_settles_a_promise_recorded_under_a_different_invoice_id(
        self, client, tmp_path,
    ):
        """The rail's invoice id and the merchant's reference are different
        namespaces. A capture arrived as `inv_TWte5TwAYXxtq8` while the
        promise was recorded against `INV-2201`, so the scoped lookup
        matched nothing, the promise stayed pending, its date passed, and a
        debtor who had actually paid was scored as having broken their word.
        """
        registry = DebtorRegistry(str(tmp_path / "debtors.db"))
        registry.record_promise("debtor_live", invoice_id="INV-2201",
                                amount_paise=42_500_00, promised_date="2026-09-05")
        registry.close()

        _post(client, "payment.captured",
              {"event": "payment.captured", "payload": {"payment": {"entity": {
                  "id": "pay_ns", "amount": 42_500_00, "status": "captured",
                  "invoice_id": "inv_SOMETHING_ELSE"}}}},
              event_id="evt_ns")

        registry = DebtorRegistry(str(tmp_path / "debtors.db"))
        try:
            outcomes = registry.outcomes_for("debtor_live")
            assert [o.outcome for o in outcomes] == ["kept"], (
                "a real payment must keep the promise even when the rail's "
                "invoice id does not match the merchant's reference")
        finally:
            registry.close()

    def test_the_same_payment_is_announced_only_once(self, client):
        """Observed live: two payment.captured events for one payment, a
        second apart, and the debtor received two identical "payment
        received" messages. INGEST dedups per (source, event_id) and the
        recovery ledger per payment_id -- neither stops a second *event*
        about the same payment producing a second message."""
        first = _post(client, "payment.captured", _captured(), event_id="evt_dup_1")
        second = _post(client, "payment.captured", _captured(), event_id="evt_dup_2")

        assert first.json()["debtor_notified"]["notified"] is True
        assert second.json()["debtor_notified"]["notified"] is False
        assert second.json()["debtor_notified"]["reason"] == "already_announced"
        assert len(_FakeTelegram.sent) == 1, "a real person was told twice"

    def test_a_different_payment_is_still_announced(self, client):
        """The claim must not silence a genuinely new capture."""
        _post(client, "payment.captured", _captured("pay_one"), event_id="evt_one")
        _post(client, "payment.captured", _captured("pay_two"), event_id="evt_two")
        assert len(_FakeTelegram.sent) == 2
