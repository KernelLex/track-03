"""MSMED Act module: all four eligibility conditions (§14.1), the due-date clock
(§14.2), and statutory interest with staleness and rounding (§14.3)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from agent.statutory.msmed import (
    EligibilityInput,
    IneligibilityReason,
    RbiBankRateConfig,
    StaleStatutoryParam,
    check_eligibility,
    compute_due_date,
    compute_statutory_interest_paise,
    is_eligible,
    load_rbi_bank_rate,
    load_trader_exclusion,
)

ELIGIBLE = EligibilityInput(
    has_valid_udyam_registration=True, udyam_category="small",
    invoice_activity_type="manufacturing", msme_status_intimated_to_buyer=True,
)


# ---- Eligibility ----


def test_fully_eligible_case():
    assert is_eligible(ELIGIBLE)
    assert check_eligibility(ELIGIBLE) == []


def test_no_udyam_registration_is_ineligible():
    inp = replace(ELIGIBLE, has_valid_udyam_registration=False)
    assert IneligibilityReason.NO_UDYAM in check_eligibility(inp)


def test_medium_category_excluded_by_43B_h():
    inp = replace(ELIGIBLE, udyam_category="medium")
    assert IneligibilityReason.MEDIUM_EXCLUDED in check_eligibility(inp)


def test_trading_activity_excluded():
    inp = replace(ELIGIBLE, invoice_activity_type="trading")
    assert IneligibilityReason.TRADING_EXCLUDED in check_eligibility(inp)


def test_status_not_intimated_is_ineligible():
    inp = replace(ELIGIBLE, msme_status_intimated_to_buyer=False)
    assert IneligibilityReason.STATUS_NOT_INTIMATED in check_eligibility(inp)


def test_multiple_failures_all_reported_at_once():
    inp = EligibilityInput(
        has_valid_udyam_registration=False, udyam_category="medium",
        invoice_activity_type="trading", msme_status_intimated_to_buyer=False,
    )
    reasons = check_eligibility(inp)
    assert len(reasons) == 4


# ---- Clock ----


def test_due_date_without_agreement_is_15_days_after_acceptance():
    due = compute_due_date(acceptance_date=date(2026, 1, 1), agreement_date=None)
    assert due == date(2026, 1, 16)


def test_due_date_with_agreement_before_45_day_ceiling_uses_agreement_date():
    due = compute_due_date(acceptance_date=date(2026, 1, 1), agreement_date=date(2026, 1, 20))
    assert due == date(2026, 1, 20)


def test_due_date_with_agreement_past_45_days_is_capped_at_the_ceiling():
    due = compute_due_date(acceptance_date=date(2026, 1, 1), agreement_date=date(2026, 4, 1))
    assert due == date(2026, 1, 1) + timedelta(days=45)


# ---- Interest ----


FRESH_RATE = RbiBankRateConfig(value=0.0550, as_of=date(2026, 8, 5), source="test", stale_after_days=120)


def test_no_interest_when_paid_on_or_before_due_date():
    assert compute_statutory_interest_paise(
        principal_paise=100_000_00, due_date=date(2026, 6, 1), payment_date=date(2026, 6, 1),
        rate_config=FRESH_RATE, today=date(2026, 8, 30),
    ) == 0
    assert compute_statutory_interest_paise(
        principal_paise=100_000_00, due_date=date(2026, 6, 1), payment_date=date(2026, 5, 20),
        rate_config=FRESH_RATE, today=date(2026, 8, 30),
    ) == 0


def test_interest_accrues_and_is_a_positive_integer_paise():
    interest = compute_statutory_interest_paise(
        principal_paise=100_000_00, due_date=date(2026, 1, 1), payment_date=date(2026, 4, 1),
        rate_config=FRESH_RATE, today=date(2026, 8, 30),
    )
    assert isinstance(interest, int)
    assert interest > 0


def test_interest_compounds_monthly_so_two_months_earns_more_than_double_one_month():
    one_month = compute_statutory_interest_paise(
        principal_paise=100_000_00, due_date=date(2026, 1, 1), payment_date=date(2026, 1, 31),
        rate_config=FRESH_RATE, today=date(2026, 8, 30),
    )
    two_months = compute_statutory_interest_paise(
        principal_paise=100_000_00, due_date=date(2026, 1, 1), payment_date=date(2026, 3, 2),
        rate_config=FRESH_RATE, today=date(2026, 8, 30),
    )
    assert two_months > one_month * 2  # compounding, not simple interest


def test_stale_bank_rate_config_raises_rather_than_computing():
    stale_rate = RbiBankRateConfig(value=0.0550, as_of=date(2025, 1, 1), source="test", stale_after_days=120)
    with pytest.raises(StaleStatutoryParam):
        compute_statutory_interest_paise(
            principal_paise=100_000_00, due_date=date(2026, 1, 1), payment_date=date(2026, 4, 1),
            rate_config=stale_rate, today=date(2026, 8, 30),
        )


def test_zero_or_negative_principal_is_rejected():
    with pytest.raises(ValueError):
        compute_statutory_interest_paise(
            principal_paise=0, due_date=date(2026, 1, 1), payment_date=date(2026, 4, 1),
            rate_config=FRESH_RATE, today=date(2026, 8, 30),
        )


def test_loads_the_committed_config_file_matching_devdoc_values():
    rate = load_rbi_bank_rate()
    assert rate.value == 0.0550
    assert rate.as_of == date(2026, 8, 5)

    trader = load_trader_exclusion()
    assert trader.applied is True
    assert trader.contested is True
