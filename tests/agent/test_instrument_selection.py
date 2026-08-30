"""Every row of DEVDOC_v6 §12.2's table as a test case, with explicit coverage at
the Rs 15,000 boundary — "Correctness at the Rs 15,000 boundary is Tier 1 measured."
"""

from __future__ import annotations

import pytest

from agent.mandate.instrument import (
    AFA_FREE_CEILING_PAISE,
    InstrumentType,
    InvalidPromise,
    Promise,
    select_instrument,
)

CEILING = AFA_FREE_CEILING_PAISE  # Rs 15,000 in paise


def test_single_payment_at_exactly_the_ceiling_is_under_it():
    spec = select_instrument(Promise(total_amount_paise=CEILING))
    assert spec.instrument == InstrumentType.UPI_AUTOPAY_ONE_TIME
    assert spec.requires_afa is False


def test_single_payment_one_paisa_over_the_ceiling_requires_afa():
    spec = select_instrument(Promise(total_amount_paise=CEILING + 1))
    assert spec.instrument == InstrumentType.UPI_BLOCK_RESERVE_PAY
    assert spec.requires_afa is True


def test_single_payment_well_under_ceiling():
    spec = select_instrument(Promise(total_amount_paise=5_00_00))  # Rs 500
    assert spec.instrument == InstrumentType.UPI_AUTOPAY_ONE_TIME


def test_single_payment_well_over_ceiling():
    spec = select_instrument(Promise(total_amount_paise=50_000_00))  # Rs 50,000
    assert spec.instrument == InstrumentType.UPI_BLOCK_RESERVE_PAY
    assert spec.requires_afa is True


def test_installments_each_at_the_ceiling_is_recurring_emandate_no_afa():
    spec = select_instrument(
        Promise(total_amount_paise=CEILING * 3, installments=3, installment_amount_paise=CEILING)
    )
    assert spec.instrument == InstrumentType.RECURRING_EMANDATE
    assert spec.requires_afa is False
    assert spec.amount_paise == CEILING


def test_installments_each_one_paisa_over_ceiling_requires_afa_per_debit():
    spec = select_instrument(
        Promise(total_amount_paise=(CEILING + 1) * 3, installments=3, installment_amount_paise=CEILING + 1)
    )
    assert spec.instrument == InstrumentType.RECURRING_EMANDATE_AFA_PER_DEBIT
    assert spec.requires_afa is True


def test_installments_without_a_per_installment_amount_refuses_to_guess():
    with pytest.raises(InvalidPromise):
        select_instrument(Promise(total_amount_paise=45_00_00, installments=3))


def test_partially_disputed_amount_never_gets_a_mandate():
    spec = select_instrument(
        Promise(total_amount_paise=100_00_00), disputed_paise=40_00_00,  # Rs 100,000 promised, Rs 40,000 disputed
    )
    assert spec.instrument == InstrumentType.PAYMENT_LINK_UNDISPUTED_PORTION
    assert spec.amount_paise == 60_00_00
    assert spec.requires_afa is False


def test_dispute_takes_priority_even_over_an_otherwise_installment_eligible_promise():
    """A disputed amount must never resolve to a mandate-shaped instrument, even
    when the promise itself looks like a clean multi-installment case."""
    spec = select_instrument(
        Promise(total_amount_paise=90_00_00, installments=3, installment_amount_paise=30_00_00),
        disputed_paise=10_00_00,
    )
    assert spec.instrument == InstrumentType.PAYMENT_LINK_UNDISPUTED_PORTION


def test_debtor_declined_instrument_falls_back_to_payment_link_and_reminder():
    spec = select_instrument(Promise(total_amount_paise=20_00_00, declined=True))
    assert spec.instrument == InstrumentType.PAYMENT_LINK_PLUS_REMINDER
    assert spec.amount_paise == 20_00_00


def test_disputed_paise_cannot_exceed_the_promised_total():
    with pytest.raises(ValueError):
        select_instrument(Promise(total_amount_paise=1000), disputed_paise=2000)


def test_negative_disputed_paise_is_rejected():
    with pytest.raises(ValueError):
        select_instrument(Promise(total_amount_paise=1000), disputed_paise=-1)
