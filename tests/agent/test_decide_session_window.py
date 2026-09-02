"""The decision gate must know a message just arrived.

Observed live on 2026-09-02. A debtor replied on WhatsApp with a concrete
instalment offer; `_decide_next_step` proposed `send_reminder`, and the
gate refused with `WHATSAPP_SESSION_WINDOW` -- while handling that very
debtor's inbound message. The context never carried `last_inbound_at`, so
rule 20 could not see the window it was being asked about, and every
message-type action on WhatsApp fell through to `escalate_human`.

Two gates ran on the same conversation and disagreed:
`_bounds_gate_followup()` set `last_inbound_at` and allowed the reply;
`_decide_next_step` did not and refused the action. The wrong one drove the
decision.

The tests that matter here are the cross-channel ones: the same message
must reach the same decision on Telegram and WhatsApp, because nothing
about the debtor's words changed.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from agent.api.demo import SCENARIOS, _decide_next_step
from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family

SCENARIO = SCENARIOS["b2b"]


def _promise() -> ExtractionResult:
    return ExtractionResult(
        family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.9,
        promise={"amount_paise": 2500000, "date": "2026-09-08"},
    )


def _liquidity() -> ExtractionResult:
    return ExtractionResult(
        family=Family.C, class_=DiagnosisClass.CASHFLOW_SHORTFALL, confidence=0.8)


class TestTheLiveFailure:
    def test_whatsapp_no_longer_refuses_a_reply_to_a_message_just_received(self):
        """The regression. Refusing here means the debtor just wrote to us
        and the gate claims the conversation window is closed."""
        decision = _decide_next_step(
            _liquidity(), SCENARIO, channel="whatsapp", debtor_key="wa_regression",
            last_inbound_at=datetime.now(),
        )
        assert "WHATSAPP_SESSION_WINDOW" not in (decision.get("refusals") or [])

    def test_without_the_timestamp_the_rule_still_refuses(self):
        """The rule itself is not weakened -- it still refuses when there is
        genuinely no inbound message to point at. This is what the bug looked
        like, kept as a test so the fix cannot be mistaken for disabling
        rule 20."""
        decision = _decide_next_step(
            _liquidity(), SCENARIO, channel="whatsapp", debtor_key="wa_no_inbound",
            last_inbound_at=None,
        )
        assert "WHATSAPP_SESSION_WINDOW" in (decision.get("refusals") or [])

    def test_an_old_inbound_message_does_not_reopen_the_window(self):
        """A reply from four days ago is outside Meta's 24-hour window, and
        must still be refused. Passing *any* timestamp is not the fix;
        passing the right one is."""
        decision = _decide_next_step(
            _liquidity(), SCENARIO, channel="whatsapp", debtor_key="wa_stale",
            last_inbound_at=datetime(2026, 1, 1) - timedelta(days=4),
        )
        assert "WHATSAPP_SESSION_WINDOW" in (decision.get("refusals") or [])


class TestTheTwoChannelsNowAgree:
    """Nothing about the debtor's words changes between channels, so nothing
    about the decision should either."""

    @pytest.mark.parametrize("extraction_factory", [_liquidity, _promise])
    def test_the_same_message_reaches_the_same_action(self, extraction_factory):
        now = datetime.now()
        telegram = _decide_next_step(
            extraction_factory(), SCENARIO, channel="telegram",
            debtor_key="agree_tg", last_inbound_at=now)
        whatsapp = _decide_next_step(
            extraction_factory(), SCENARIO, channel="whatsapp",
            debtor_key="agree_wa", last_inbound_at=now)
        assert telegram["action"] == whatsapp["action"]

    def test_whatsapp_is_not_permanently_escalating(self):
        """The user-visible symptom: every WhatsApp exchange ended with a
        human handoff, so the channel could never act on its own."""
        decision = _decide_next_step(
            _liquidity(), SCENARIO, channel="whatsapp", debtor_key="wa_autonomy",
            last_inbound_at=datetime.now(),
        )
        assert decision["action"] != "escalate_human"


class TestItDidNotWeakenAnythingElse:
    def test_a_dispute_still_goes_to_a_human(self):
        """Family D must escalate whatever the channel or the window says.
        If a timestamp could change this, the fix would have broken the one
        rule that matters most."""
        dispute = ExtractionResult(
            family=Family.D, class_=DiagnosisClass.NOT_OUR_DEBT, confidence=0.9)
        decision = _decide_next_step(
            dispute, SCENARIO, channel="whatsapp", debtor_key="wa_dispute",
            last_inbound_at=datetime.now(),
        )
        assert decision["action"] == "escalate_human"

    def test_telegram_is_unaffected_by_the_parameter(self):
        """Rule 20 only applies to WhatsApp, so Telegram's decision must be
        identical with or without the timestamp -- proving the change is
        scoped to the channel that had the bug."""
        with_ts = _decide_next_step(
            _liquidity(), SCENARIO, channel="telegram", debtor_key="tg_scope_a",
            last_inbound_at=datetime.now())
        without = _decide_next_step(
            _liquidity(), SCENARIO, channel="telegram", debtor_key="tg_scope_b",
            last_inbound_at=None)
        assert with_ts["action"] == without["action"]

    def test_the_gate_verdict_is_still_reported_honestly(self):
        """`allowed` must reflect what the gate said, not what was wanted.
        A previous bug reported allowed=True for a refused action
        (WHAT_BROKE #20)."""
        decision = _decide_next_step(
            _liquidity(), SCENARIO, channel="whatsapp", debtor_key="wa_honest",
            last_inbound_at=None,
        )
        assert decision["allowed"] is False
        assert decision["action"] == "escalate_human"
        assert decision["proposed_action"] != "escalate_human"
