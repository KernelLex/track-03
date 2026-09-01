"""Tests for eval.personas.adversarial.strategies -- DEVDOC_v6 §24.3's
three mechanically-distinct adversarial personas, run against the real
check_bounds() gate (not a stand-in). The one property that matters: none
of them permanently stall a case."""

from __future__ import annotations

from eval.personas.adversarial.strategies import (
    AdversarialStrategy,
    run_channel_hopper,
    run_dispute_abuser,
    run_serial_promiser,
)

AMOUNT_PAISE = 50_000_00


class TestSerialPromiser:
    def test_never_permanently_stalls_over_a_long_window(self):
        result = run_serial_promiser("p1", window_days=180, amount_paise=AMOUNT_PAISE)
        assert result.permanently_stalled is False
        assert result.ever_recontacted_or_escalated is True

    def test_first_attempt_always_succeeds_no_history_yet(self):
        result = run_serial_promiser("p1", window_days=30, amount_paise=AMOUNT_PAISE)
        assert result.attempts[0].allowed is True

    def test_strategy_is_tagged_correctly(self):
        result = run_serial_promiser("p1", window_days=10, amount_paise=AMOUNT_PAISE)
        assert result.strategy == AdversarialStrategy.SERIAL_PROMISER

    def test_recontact_eventually_happens_again_after_a_broken_promise(self):
        """The actual exploit this rule closes: a full, fixed cooldown per
        promise would mean exactly one contact ever, permanently. Credibility
        decay must allow at least a second real contact within a reasonable window."""
        result = run_serial_promiser("p1", window_days=60, amount_paise=AMOUNT_PAISE)
        successes = [a for a in result.attempts if a.allowed]
        assert len(successes) >= 2


class TestDisputeAbuser:
    def test_never_permanently_stalls(self):
        result = run_dispute_abuser("p2", window_days=90, amount_paise=AMOUNT_PAISE)
        assert result.permanently_stalled is False

    def test_escalate_human_passes_on_first_dispute(self):
        result = run_dispute_abuser("p2", window_days=10, amount_paise=AMOUNT_PAISE)
        escalations = [a for a in result.attempts if a.action_type == "escalate_human"]
        assert escalations[0].allowed is True

    def test_plain_reminder_is_refused_by_dispute_freeze_after_first_contact(self):
        result = run_dispute_abuser("p2", window_days=15, amount_paise=AMOUNT_PAISE)
        reminders = [a for a in result.attempts if a.action_type == "send_reminder"]
        # The second reminder attempt (first happens before the dispute state is set)
        assert any("DISPUTE_FREEZE" in a.refusal_reasons for a in reminders)


class TestChannelHopper:
    def test_never_permanently_stalls(self):
        result = run_channel_hopper("p3", window_days=90, amount_paise=AMOUNT_PAISE)
        assert result.permanently_stalled is False

    def test_reminder_attempts_succeed_on_each_new_channel(self):
        result = run_channel_hopper("p3", window_days=40, amount_paise=AMOUNT_PAISE)
        reminder_attempts = [a for a in result.attempts if a.action_type.startswith("send_reminder")]
        assert all(a.allowed for a in reminder_attempts)

    def test_escalation_succeeds_once_every_channel_is_exhausted(self):
        result = run_channel_hopper("p3", window_days=90, amount_paise=AMOUNT_PAISE)
        escalations = [a for a in result.attempts if a.action_type == "escalate_human"]
        assert escalations
        assert escalations[0].allowed is True


class TestNoStrategyEverPermanentlyStalls:
    """The single number DEVDOC_v6 §24.3 actually asks for, checked across
    a handful of different personas/windows, not just one hand-picked case."""

    def test_across_several_personas_and_windows(self):
        for persona_id in ["a", "b", "c"]:
            for window in [30, 90, 180]:
                assert run_serial_promiser(persona_id, window_days=window, amount_paise=AMOUNT_PAISE).permanently_stalled is False
                assert run_dispute_abuser(persona_id, window_days=window, amount_paise=AMOUNT_PAISE).permanently_stalled is False
                assert run_channel_hopper(persona_id, window_days=window, amount_paise=AMOUNT_PAISE).permanently_stalled is False
