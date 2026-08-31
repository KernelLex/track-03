"""Tests for agent.decide.payday_signal -- the free, already-owned
alternative to a live balance check: a debtor's own past captured-payment
dates, grouped by day-of-month, sourced entirely from recovery_ledger.
"""

from __future__ import annotations

import pytest

from agent.decide.payday_signal import PaydaySignal, compute_payday_signal
from agent.ledger.recovery import RecoveryLedger


@pytest.fixture
def ledger(tmp_path):
    with RecoveryLedger(tmp_path / "recovery.db") as rl:
        yield rl


def _attribute(ledger, *, payment_id, debtor_id, day, month="03", year="2026", amount_paise=10_000):
    ledger.attribute(
        payment_id=payment_id, payment_status="captured", invoice_id=f"inv_{payment_id}",
        debtor_id=debtor_id, amount_paise=amount_paise, rail_tag="simulated",
        recorded_at=f"{year}-{month}-{day:02d}T10:00:00.000000Z",
    )


class TestNoOrSparseHistory:
    def test_no_history_yields_zero_confidence_empty_signal(self, ledger):
        signal = compute_payday_signal(ledger, "debtor_new")
        assert signal.sample_size == 0
        assert signal.observed_days == ()
        assert signal.likely_days == ()
        assert signal.confidence == 0.0
        assert signal.favors(5) is False

    def test_a_single_past_capture_is_not_treated_as_a_pattern(self, ledger):
        _attribute(ledger, payment_id="p1", debtor_id="d1", day=5)
        signal = compute_payday_signal(ledger, "d1")
        assert signal.sample_size == 1
        assert signal.likely_days == ()  # one data point isn't a recurring day
        assert signal.confidence == 0.0

    def test_two_captures_on_different_days_yield_no_recurring_pattern(self, ledger):
        _attribute(ledger, payment_id="p1", debtor_id="d1", day=5)
        _attribute(ledger, payment_id="p2", debtor_id="d1", day=17, month="04")
        signal = compute_payday_signal(ledger, "d1")
        assert signal.sample_size == 2
        assert signal.likely_days == ()
        assert signal.confidence == 0.0


class TestRecurringPattern:
    def test_a_day_appearing_twice_becomes_a_likely_day(self, ledger):
        _attribute(ledger, payment_id="p1", debtor_id="d1", day=5)
        _attribute(ledger, payment_id="p2", debtor_id="d1", day=5, month="04")
        signal = compute_payday_signal(ledger, "d1")
        assert signal.likely_days == (5,)
        assert signal.confidence == 1.0
        assert signal.favors(5) is True
        assert signal.favors(6) is False

    def test_most_frequent_day_sorts_first(self, ledger):
        for i, day in enumerate([5, 5, 5, 20, 20]):
            _attribute(ledger, payment_id=f"p{i}", debtor_id="d1", day=day, month=f"{(i % 9) + 1:02d}")
        signal = compute_payday_signal(ledger, "d1")
        assert signal.likely_days[0] == 5
        assert 20 in signal.likely_days

    def test_confidence_reflects_the_share_of_captures_on_a_likely_day(self, ledger):
        # Two captures on day 5 (recurring), one lone capture on day 12 (not recurring).
        _attribute(ledger, payment_id="p1", debtor_id="d1", day=5, month="01")
        _attribute(ledger, payment_id="p2", debtor_id="d1", day=5, month="02")
        _attribute(ledger, payment_id="p3", debtor_id="d1", day=12, month="03")
        signal = compute_payday_signal(ledger, "d1")
        assert signal.likely_days == (5,)
        assert signal.sample_size == 3
        assert signal.confidence == pytest.approx(2 / 3)

    def test_observed_days_preserves_chronological_order_and_every_data_point(self, ledger):
        _attribute(ledger, payment_id="p1", debtor_id="d1", day=20, month="01")
        _attribute(ledger, payment_id="p2", debtor_id="d1", day=5, month="02")
        signal = compute_payday_signal(ledger, "d1")
        assert signal.observed_days == (20, 5)  # insertion/chronological order, not sorted


class TestDebtorIsolation:
    def test_one_debtors_history_never_leaks_into_anothers_signal(self, ledger):
        _attribute(ledger, payment_id="p1", debtor_id="d1", day=5, month="01")
        _attribute(ledger, payment_id="p2", debtor_id="d1", day=5, month="02")
        _attribute(ledger, payment_id="p3", debtor_id="d2", day=9, month="01")
        signal_d1 = compute_payday_signal(ledger, "d1")
        signal_d2 = compute_payday_signal(ledger, "d2")
        assert signal_d1.likely_days == (5,)
        assert signal_d2.sample_size == 1
        assert signal_d2.likely_days == ()


def test_payday_signal_is_a_real_dataclass_instance():
    assert PaydaySignal.__dataclass_fields__.keys() >= {"debtor_id", "sample_size", "observed_days", "likely_days", "confidence"}
