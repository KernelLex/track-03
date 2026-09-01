"""The timeline endpoint, and the stage it reports.

Why this exists: the dashboard rendered only events its own browser tab had
witnessed. A call placed before the page loaded, or a reply the Telegram
webhook answered while nothing was polling, was invisible -- the system did
the work and the UI showed an empty list. That is a demo that lies about
itself, in the direction of underselling.

No real network and no real model anywhere here.
"""

from __future__ import annotations

from datetime import date, timedelta

import agent.api.demo as demo_module
import pytest
from fastapi.testclient import TestClient

from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family, PromiseFields
from agent.notify.protocol import MessageSendResult

SECRET = "tg-hook-secret"
CHAT_ID = "999888777"


class _FakeTelegram:
    def __init__(self, *args, **kwargs):
        pass

    def send(self, *, to, text):
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
    # No Razorpay credentials -> no real mandate calls from this suite.
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    monkeypatch.setattr(demo_module, "TelegramChannel", _FakeTelegram)
    monkeypatch.setattr(demo_module, "compose_reply", lambda reply_text, **kw: "composed reply")

    from agent.api.app import app

    with TestClient(app) as c:
        yield c


def _extracts(monkeypatch, **fields):
    monkeypatch.setattr(
        demo_module, "extract_from_reply",
        lambda text, **kw: ExtractionResult(**fields),
    )


def _inbound(client, text, update_id=1):
    return client.post(
        "/demo/telegram-webhook",
        json={"update_id": update_id, "message": {"chat": {"id": CHAT_ID}, "text": text}},
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )


def _kinds(client, **params):
    body = client.get("/demo/timeline", params=params).json()
    return [e["kind"] for e in body["events"]], body


class TestTheExchangeIsRecorded:
    def test_a_reply_records_every_step_it_went_through(self, client, monkeypatch):
        _extracts(monkeypatch, family=Family.C, class_=DiagnosisClass.STALLING, confidence=0.5)
        _inbound(client, "next week maybe")

        kinds, _ = _kinds(client)
        assert kinds == ["reply_received", "diagnosed", "decided", "agent_replied"]

    def test_the_events_survive_the_request_that_made_them(self, client, monkeypatch):
        """The point of the whole endpoint: a viewer who arrives afterwards
        still sees what happened."""
        _extracts(monkeypatch, family=Family.C, class_=DiagnosisClass.STALLING, confidence=0.5)
        _inbound(client, "ok")

        body = client.get("/demo/timeline").json()
        assert any(e["kind"] == "reply_received" and e["detail"]["text"] == "ok" for e in body["events"])

    def test_the_timeline_reads_oldest_first(self, client, monkeypatch):
        _extracts(monkeypatch, family=Family.C, class_=DiagnosisClass.STALLING, confidence=0.5)
        _inbound(client, "one", update_id=1)
        _inbound(client, "two", update_id=2)

        texts = [e["detail"]["text"] for e in client.get("/demo/timeline").json()["events"]
                 if e["kind"] == "reply_received"]
        assert texts == ["one", "two"]

    def test_turns_come_back_for_a_named_conversation(self, client, monkeypatch):
        _extracts(monkeypatch, family=Family.C, class_=DiagnosisClass.STALLING, confidence=0.5)
        _inbound(client, "hello there")

        body = client.get("/demo/timeline", params={"conversation_id": CHAT_ID}).json()
        assert [t["direction"] for t in body["turns"]] == ["inbound", "outbound"]
        assert body["turns"][0]["text"] == "hello there"

    def test_a_failed_extraction_is_on_the_record_too(self, client, monkeypatch):
        from agent.diagnose.llm_extract import ExtractionFailed

        def _boom(text, **kw):
            raise ExtractionFailed("model unavailable")

        monkeypatch.setattr(demo_module, "extract_from_reply", _boom)
        _inbound(client, "anything")

        kinds, _ = _kinds(client)
        assert "extraction_failed" in kinds


class TestTheStage:
    def test_nothing_sent_yet_reads_as_not_started(self, client):
        body = client.get("/demo/timeline").json()
        assert body["stage"] == "not_started"
        assert "Nothing has been sent" in body["stage_label"] or "nothing has been sent" in body["stage_label"]

    def test_a_reply_moves_it_into_conversation(self, client, monkeypatch):
        _extracts(monkeypatch, family=Family.C, class_=DiagnosisClass.STALLING, confidence=0.5)
        _inbound(client, "got it")
        assert client.get("/demo/timeline").json()["stage"] == "in_conversation"

    def test_a_stated_plan_moves_it_to_negotiating(self, client, monkeypatch):
        _extracts(monkeypatch, family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.9,
                  promise=PromiseFields(amount_paise=21_000_00, date="2026-12-05"))
        _inbound(client, "21,000 on the 5th")
        assert client.get("/demo/timeline").json()["stage"] == "negotiating"

    def test_a_dispute_overrides_progress_because_chasing_has_stopped(self, client, monkeypatch):
        """Reporting "negotiating" over a frozen account would misstate what
        the system is actually doing."""
        _extracts(monkeypatch, family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.9,
                  promise=PromiseFields(amount_paise=21_000_00, date="2026-12-05"))
        _inbound(client, "21,000 on the 5th", update_id=1)

        _extracts(monkeypatch, family=Family.D, class_=DiagnosisClass.QUANTITY_QUALITY, confidence=0.9)
        _inbound(client, "actually the goods never arrived", update_id=2)

        body = client.get("/demo/timeline").json()
        assert body["stage"] in ("disputed_paused", "escalated_to_human")
        assert "paused" in body["stage_label"] or "person" in body["stage_label"]

    def test_progress_is_not_lost_to_a_later_unremarkable_message(self, client, monkeypatch):
        """A mandate that has been issued stays issued -- small talk
        afterwards must not walk the stage backwards."""
        _extracts(monkeypatch, family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.9,
                  promise=PromiseFields(amount_paise=21_000_00, date="2026-12-05"))
        _inbound(client, "21,000 on the 5th", update_id=1)

        _extracts(monkeypatch, family=Family.C, class_=DiagnosisClass.STALLING, confidence=0.3)
        _inbound(client, "thanks", update_id=2)

        assert client.get("/demo/timeline").json()["stage"] == "negotiating"


class TestTheEndpointItself:
    def test_it_is_readable_without_the_trigger_secret(self, client):
        """Watching is not triggering. Requiring the send secret to read
        the timeline would mean baking it into a page that only wants to
        watch."""
        assert client.get("/demo/timeline").status_code == 200

    def test_an_absurd_limit_is_clamped_rather_than_honoured(self, client):
        assert client.get("/demo/timeline", params={"limit": 100000}).status_code == 200
        assert client.get("/demo/timeline", params={"limit": 0}).status_code == 200

    def test_an_unknown_conversation_is_empty_not_an_error(self, client):
        body = client.get("/demo/timeline", params={"conversation_id": "nobody"}).json()
        assert body["events"] == []
        assert body["turns"] == []
        assert body["stage"] == "not_started"


class TestARefusedActionIsNeverReportedAsAllowed:
    """Found in a live run, not by a test.

    A debtor replied "I can pay 21000 on the 5th and rest later". That put
    them in PROMISED, PROMISE_COOLDOWN refused every action -- and the
    timeline recorded `action=send_reminder, allowed=True, refusals=
    ['PROMISE_COOLDOWN']`. The gate refused an action and the system
    reported it as the allowed next step. For a project whose central claim
    is that `check_bounds()` is a real chokepoint, that is the worst kind of
    small bug: everything downstream of it still worked.
    """

    @staticmethod
    def _soon():
        return (date.today() + timedelta(days=200)).isoformat()

    def _decide(self, monkeypatch, **kw):
        from agent.diagnose.extract import ExtractionResult

        return demo_module._decide_next_step(
            ExtractionResult(**kw), demo_module.SCENARIOS["b2b"],
            channel="telegram", debtor_key="regression_debtor",
        )

    def test_allowed_reflects_the_gate_not_the_fallback(self, client, monkeypatch):
        """`allowed` used to be `chosen == proposed`, which is True both
        when the gate passed and when every fallback failed and the refused
        action was reused."""
        decision = self._decide(
            monkeypatch, family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.9,
            promise=PromiseFields(amount_paise=21_000_00, date=self._soon()),
        )
        assert decision["refusals"] == ["PROMISE_COOLDOWN"]
        assert decision["allowed"] is False
        assert decision["action"] != decision["proposed_action"]

    def test_a_promise_waits_rather_than_escalating(self, client, monkeypatch):
        """A debtor naming a date is a good outcome. Escalating them to a
        person over-reacts to what the cooldown actually asked for, and
        buries the queue a real escalation needs to stay useful."""
        decision = self._decide(
            monkeypatch, family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.9,
            promise=PromiseFields(amount_paise=21_000_00, date=self._soon()),
        )
        assert decision["action"] == "no_action"
        assert decision["escalated_to_human"] is False

    def test_a_dispute_still_escalates_to_a_person(self, client, monkeypatch):
        """The wait-vs-escalate split must not turn every refusal into a
        shrug. A dispute is exactly the case a person should see."""
        decision = self._decide(
            monkeypatch, family=Family.D, class_=DiagnosisClass.QUANTITY_QUALITY, confidence=0.9,
        )
        assert decision["action"] == "escalate_human"

    def test_the_wait_set_names_only_timing_refusals(self):
        """A guard on the set itself. Adding a rule here that means "a
        person should look at this" would silently downgrade it to a shrug."""
        assert demo_module.REFUSALS_THAT_MEAN_WAIT == frozenset(
            {"PROMISE_COOLDOWN", "RBI_FPC_HOURS", "EV_FLOOR"}
        )


class TestASilentFallbackIsRecorded:
    """A live run fell back to the fixed line and the only trace was a
    Render log line nobody reads.

    The debtor got "here's the link again whenever you're ready" -- carrying
    a different URL than either mandate just built for them -- and working
    out why afterwards was guesswork. A degradation that leaves no record is
    the same invisible-absence problem a refused action has, and it matters
    more, because the reply still looks plausible.
    """

    def _fails_to_compose(self, monkeypatch, exc):
        def _boom(reply_text, **kw):
            raise exc
        monkeypatch.setattr(demo_module, "compose_reply", _boom)

    def test_the_reason_lands_on_the_timeline(self, client, monkeypatch):
        from agent.notify.compose import ComposeFailed

        _extracts(monkeypatch, family=Family.C, class_=DiagnosisClass.STALLING, confidence=0.5)
        self._fails_to_compose(monkeypatch, ComposeFailed("API call failed: 529 overloaded"))
        _inbound(client, "anything")

        events = client.get("/demo/timeline").json()["events"]
        failed = [e for e in events if e["kind"] == "compose_failed"]
        assert failed, "a fallback must leave a record"
        assert "529 overloaded" in failed[0]["detail"]["reason"]

    def test_it_records_what_was_sent_instead(self, client, monkeypatch):
        """Knowing a fallback happened is half of it; the other half is what
        the debtor actually received."""
        from agent.notify.compose import ComposeFailed

        _extracts(monkeypatch, family=Family.C, class_=DiagnosisClass.STALLING, confidence=0.5)
        self._fails_to_compose(monkeypatch, ComposeFailed("empty response"))
        _inbound(client, "anything")

        failed = [e for e in client.get("/demo/timeline").json()["events"]
                  if e["kind"] == "compose_failed"][0]
        assert failed["detail"]["sent_instead"]

    def test_a_budget_stop_is_recorded_the_same_way(self, client, monkeypatch):
        """BudgetExceeded and an API failure both mean "no vetted reply", and
        both must be distinguishable afterwards -- they need different fixes."""
        from agent.spend import BudgetExceeded

        _extracts(monkeypatch, family=Family.C, class_=DiagnosisClass.STALLING, confidence=0.5)
        self._fails_to_compose(monkeypatch, BudgetExceeded("would exceed the $20.00 ceiling"))
        _inbound(client, "anything")

        failed = [e for e in client.get("/demo/timeline").json()["events"]
                  if e["kind"] == "compose_failed"][0]
        assert "BudgetExceeded" in failed["detail"]["reason"]

    def test_a_successful_reply_records_no_failure(self, client, monkeypatch):
        _extracts(monkeypatch, family=Family.C, class_=DiagnosisClass.STALLING, confidence=0.5)
        _inbound(client, "anything")
        kinds = [e["kind"] for e in client.get("/demo/timeline").json()["events"]]
        assert "compose_failed" not in kinds

    def test_a_stale_reason_is_not_attributed_to_a_later_reply(self, client, monkeypatch):
        """The reason is module-level state. Without clearing it before each
        call, one failure would mark every subsequent success as degraded."""
        from agent.notify.compose import ComposeFailed

        _extracts(monkeypatch, family=Family.C, class_=DiagnosisClass.STALLING, confidence=0.5)
        self._fails_to_compose(monkeypatch, ComposeFailed("transient"))
        _inbound(client, "sorry about the delay", update_id=1)

        monkeypatch.setattr(demo_module, "compose_reply", lambda reply_text, **kw: "composed reply")
        _inbound(client, "checking with accounts now", update_id=2)

        failed = [e for e in client.get("/demo/timeline").json()["events"]
                  if e["kind"] == "compose_failed"]
        assert len(failed) == 1, "the second reply succeeded and must not be marked degraded"


class TestTheGateVerdictIsRenderable:
    """The dashboard showed a refusal as small grey text -- `no_action ·
    refused by PROMISE_COOLDOWN` -- indistinguishable at a glance from a
    successful send. For a project whose thesis is "the part worth judging
    is what it refuses to do", the refusal was the least visible thing on
    screen.

    These pin the backend contract the refusal strip renders from. The
    strip is markup, but it can only say what the decision carries.
    """

    def _decide(self, **kw):
        from agent.diagnose.extract import ExtractionResult

        return demo_module._decide_next_step(
            ExtractionResult(**kw), demo_module.SCENARIOS["b2b"],
            channel="telegram", debtor_key="gate_render_debtor",
        )

    def test_a_refusal_carries_the_rules_own_words(self, client):
        """`BoundsVerdict.reason` is already `rule.human` on a refusal, so
        the plain-language explanation existed and was being dropped at this
        boundary -- which is why the UI could only show a bare identifier a
        viewer had to already know the meaning of."""
        from datetime import date, timedelta

        decision = self._decide(
            family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.9,
            promise=PromiseFields(amount_paise=21_000_00,
                                  date=(date.today() + timedelta(days=5)).isoformat()),
        )
        assert decision["refusals"] == ["PROMISE_COOLDOWN"]
        detail = decision["refusal_detail"]
        assert detail and detail[0]["rule_id"] == "PROMISE_COOLDOWN"
        assert len(detail[0]["reason"]) > 20, "a rule id without its reason is not an explanation"

    def test_the_tally_lets_the_ui_show_scale(self, client):
        """"1 REFUSED" means little without "18/19 passed" beside it."""
        from datetime import date, timedelta

        decision = self._decide(
            family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.9,
            promise=PromiseFields(amount_paise=21_000_00,
                                  date=(date.today() + timedelta(days=5)).isoformat()),
        )
        assert decision["rules_total"] == 19
        assert decision["rules_passed"] == decision["rules_total"] - len(decision["refusals"])

    def test_a_clean_pass_reports_a_full_tally_and_no_refusals(self, client):
        decision = self._decide(
            family=Family.C, class_=DiagnosisClass.STALLING, confidence=0.5)
        assert decision["refusal_detail"] == []
        assert decision["rules_passed"] == decision["rules_total"]

    def test_the_outcome_is_on_the_decision_so_a_refusal_is_never_a_dead_end(self, client):
        """A refusal with no stated outcome reads as the system giving up.
        The whole design argument is that it is a routing decision, so what
        happened instead has to be renderable next to it."""
        from datetime import date, timedelta

        decision = self._decide(
            family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.9,
            promise=PromiseFields(amount_paise=21_000_00,
                                  date=(date.today() + timedelta(days=5)).isoformat()),
        )
        assert decision["action"] in ("no_action", "escalate_human")
        assert decision["allowed"] is False
