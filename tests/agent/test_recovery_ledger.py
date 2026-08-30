"""SETTLE stage / Law 7: recovery_ledger's UNIQUE(payment_id) is the actual defense. DEVDOC_v6 §9.3, §16."""

from __future__ import annotations

import pytest

from agent.ledger.recovery import NotCaptured, RecoveryLedger


@pytest.fixture
def ledger(tmp_path):
    with RecoveryLedger(tmp_path / "recovery.db") as rl:
        yield rl


def test_attribution_of_a_captured_payment_succeeds(ledger):
    entry = ledger.attribute(
        payment_id="pay_1", payment_status="captured", invoice_id="inv_1",
        debtor_id="debtor_1", amount_paise=50_000, rail_tag="simulated",
    )
    assert entry is not None
    assert entry.amount_paise == 50_000
    assert ledger.total_recovered_paise() == 50_000


@pytest.mark.parametrize("status", ["authorized", "created", "pending", "failed"])
def test_non_captured_status_is_refused_not_recovered(ledger, status):
    with pytest.raises(NotCaptured):
        ledger.attribute(
            payment_id="pay_1", payment_status=status, invoice_id="inv_1",
            debtor_id="debtor_1", amount_paise=50_000, rail_tag="simulated",
        )
    assert ledger.total_recovered_paise() == 0


def test_double_attribution_of_the_same_payment_id_is_rejected_by_the_constraint(ledger):
    first = ledger.attribute(
        payment_id="pay_dup", payment_status="captured", invoice_id="inv_1",
        debtor_id="debtor_1", amount_paise=10_000, rail_tag="simulated",
    )
    second = ledger.attribute(
        payment_id="pay_dup", payment_status="captured", invoice_id="inv_1",
        debtor_id="debtor_1", amount_paise=10_000, rail_tag="simulated",
    )
    assert first is not None
    assert second is None  # rejected, not double-counted
    assert ledger.total_recovered_paise() == 10_000  # counted exactly once


def test_partial_recovery_rolls_up_per_invoice(ledger):
    """§16: an invoice settled 40% through an installment mandate is 40% recovered —
    multiple payment_ids can attribute to the same invoice_id."""
    ledger.attribute(payment_id="pay_a", payment_status="captured", invoice_id="inv_installment",
                      debtor_id="debtor_1", amount_paise=4_000, rail_tag="simulated")
    ledger.attribute(payment_id="pay_b", payment_status="captured", invoice_id="inv_installment",
                      debtor_id="debtor_1", amount_paise=6_000, rail_tag="simulated")

    assert ledger.total_recovered_paise(invoice_id="inv_installment") == 10_000
    assert len(ledger.entries_for_invoice("inv_installment")) == 2


def test_totals_filter_by_arm_for_the_four_arm_eval(ledger):
    ledger.attribute(payment_id="pay_arm_a", payment_status="captured", invoice_id="inv_1",
                      debtor_id="debtor_1", amount_paise=1_000, rail_tag="simulated", arm="a")
    ledger.attribute(payment_id="pay_arm_c", payment_status="captured", invoice_id="inv_2",
                      debtor_id="debtor_2", amount_paise=2_000, rail_tag="simulated", arm="c")

    assert ledger.total_recovered_paise(arm="a") == 1_000
    assert ledger.total_recovered_paise(arm="c") == 2_000
    assert ledger.total_recovered_paise() == 3_000


def test_zero_or_negative_amount_is_rejected(ledger):
    with pytest.raises(ValueError):
        ledger.attribute(payment_id="pay_bad", payment_status="captured", invoice_id="inv_1",
                          debtor_id="debtor_1", amount_paise=0, rail_tag="simulated")
