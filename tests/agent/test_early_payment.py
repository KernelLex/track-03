"""Tests for agent.mandate.early_payment -- the early-payment discount, the
B2B-side complement to agent.statutory.msmed's late-fee calculation."""

from __future__ import annotations

from datetime import date

import pytest

from agent.decide.ev import Prior
from agent.mandate.early_payment import (
    DEFAULT_DISCOUNT_RATE,
    DEFAULT_EARLY_PAYMENT_LIFT,
    DEFAULT_WINDOW_DAYS,
    EarlyPaymentOffer,
    InvalidDiscountTerms,
    compare_full_price_vs_discount_ev,
    compute_early_payment_offer,
)


class TestComputeEarlyPaymentOffer:
    def test_default_terms_compute_a_two_percent_discount(self):
        offer = compute_early_payment_offer(
            invoice_id="INV-1", amount_paise=100_000_00, offer_date=date(2026, 9, 1),
        )
        assert offer.discount_rate == DEFAULT_DISCOUNT_RATE
        assert offer.savings_paise == 200_000  # 2% of 100,000.00
        assert offer.discounted_amount_paise == 98_000_00
        assert offer.discount_valid_until == date(2026, 9, 1 + DEFAULT_WINDOW_DAYS)

    def test_custom_terms_are_respected(self):
        offer = compute_early_payment_offer(
            invoice_id="INV-2", amount_paise=50_000_00, offer_date=date(2026, 1, 1),
            discount_rate=0.05, window_days=5,
        )
        assert offer.savings_paise == 2_500_00
        assert offer.discounted_amount_paise == 47_500_00
        assert offer.discount_valid_until == date(2026, 1, 6)

    def test_discount_plus_discounted_amount_equals_original(self):
        offer = compute_early_payment_offer(
            invoice_id="INV-3", amount_paise=73_333_00, offer_date=date(2026, 3, 15), discount_rate=0.03,
        )
        assert offer.discounted_amount_paise + offer.savings_paise == offer.original_amount_paise

    def test_zero_amount_is_rejected(self):
        with pytest.raises(ValueError):
            compute_early_payment_offer(invoice_id="x", amount_paise=0, offer_date=date(2026, 1, 1))

    def test_negative_amount_is_rejected(self):
        with pytest.raises(ValueError):
            compute_early_payment_offer(invoice_id="x", amount_paise=-500, offer_date=date(2026, 1, 1))

    @pytest.mark.parametrize("bad_rate", [-0.1, 1.0, 1.5])
    def test_discount_rate_out_of_range_is_rejected(self, bad_rate):
        with pytest.raises(InvalidDiscountTerms):
            compute_early_payment_offer(invoice_id="x", amount_paise=10_000, offer_date=date(2026, 1, 1), discount_rate=bad_rate)

    def test_zero_or_negative_window_is_rejected(self):
        with pytest.raises(InvalidDiscountTerms):
            compute_early_payment_offer(invoice_id="x", amount_paise=10_000, offer_date=date(2026, 1, 1), window_days=0)

    def test_zero_discount_rate_is_a_valid_no_op_offer(self):
        offer = compute_early_payment_offer(invoice_id="x", amount_paise=10_000, offer_date=date(2026, 1, 1), discount_rate=0.0)
        assert offer.savings_paise == 0
        assert offer.discounted_amount_paise == 10_000


class TestOfferValidity:
    def test_offer_is_valid_within_window(self):
        offer = compute_early_payment_offer(invoice_id="x", amount_paise=10_000_00, offer_date=date(2026, 1, 1), window_days=10)
        assert offer.is_valid(as_of=date(2026, 1, 1)) is True
        assert offer.is_valid(as_of=date(2026, 1, 11)) is True  # inclusive of the boundary day

    def test_offer_is_invalid_after_window(self):
        offer = compute_early_payment_offer(invoice_id="x", amount_paise=10_000_00, offer_date=date(2026, 1, 1), window_days=10)
        assert offer.is_valid(as_of=date(2026, 1, 12)) is False


class TestCompareFullPriceVsDiscountEv:
    def test_at_default_lift_discount_is_never_better(self):
        """The honest default: with no assumed behavioural uplift, giving
        away money for the same probability of payment is always worse."""
        comparison = compare_full_price_vs_discount_ev(
            p_base=0.9, chase_lift_prior=Prior(1.0),
            full_amount_paise=100_000_00, discounted_amount_paise=98_000_00, cost_paise=500,
        )
        assert comparison.discount_is_better is False
        assert comparison.discounted_decision.ev_paise < comparison.full_price_decision.ev_paise

    def test_default_lift_is_neutral_one(self):
        assert DEFAULT_EARLY_PAYMENT_LIFT == Prior(1.0)

    def test_a_sufficiently_large_declared_lift_makes_discount_better(self):
        """A caller asserting a real behavioural uplift (not this module's
        default) can make the discounted path win -- proving the lift is
        genuinely load-bearing, exactly as declared, not hidden."""
        comparison = compare_full_price_vs_discount_ev(
            p_base=0.5, chase_lift_prior=Prior(1.0),
            full_amount_paise=100_000_00, discounted_amount_paise=98_000_00, cost_paise=500,
            early_payment_lift=Prior(1.5),
        )
        assert comparison.discount_is_better is True

    def test_uses_the_same_p_base_for_both_paths(self):
        """p_base is the fitted half of EV -- the discount's effect is
        modelled entirely through the lift, not by also perturbing p_base."""
        comparison = compare_full_price_vs_discount_ev(
            p_base=0.75, chase_lift_prior=Prior(1.0),
            full_amount_paise=50_000_00, discounted_amount_paise=49_000_00, cost_paise=500,
        )
        assert comparison.full_price_decision.p_base == 0.75
        assert comparison.discounted_decision.p_base == 0.75

    def test_returns_real_decision_objects_from_compute_ev(self):
        comparison = compare_full_price_vs_discount_ev(
            p_base=0.9, chase_lift_prior=Prior(1.0),
            full_amount_paise=10_000_00, discounted_amount_paise=9_800_00, cost_paise=500,
        )
        assert comparison.full_price_decision.recoverable_paise == 10_000_00
        assert comparison.discounted_decision.recoverable_paise == 9_800_00
