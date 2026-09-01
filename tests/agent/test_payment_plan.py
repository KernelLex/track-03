"""Tests for agent.mandate.payment_plan -- the join between a debtor's own
proposed split, the instrument rules, and the early-payment pricing.

Pure arithmetic and pure selection: no network, no model, no rail.
"""

from __future__ import annotations

from datetime import date

import pytest

from agent.mandate.instrument import InstrumentType
from agent.mandate.payment_plan import PlanRejected, build_plan, describe_plan

TODAY = date(2026, 9, 1)


def _two_legs():
    return [(21_250_00, date(2026, 9, 5)), (21_250_00, date(2026, 9, 20))]


class TestPlanShape:
    def test_legs_are_ordered_by_due_date_not_by_how_they_were_said(self):
        """A debtor rarely proposes dates in order."""
        plan = build_plan(
            invoice_id="INV-1", total_amount_paise=42_500_00,
            legs=[(21_250_00, date(2026, 9, 20)), (21_250_00, date(2026, 9, 5))], today=TODAY,
        )
        assert [leg.due_date for leg in plan.legs] == [date(2026, 9, 5), date(2026, 9, 20)]
        assert [leg.sequence for leg in plan.legs] == [1, 2]

    def test_an_uneven_split_is_preserved_not_averaged(self):
        """A real plan need not divide evenly, and rewriting what they
        offered into equal parts would misrepresent it."""
        plan = build_plan(
            invoice_id="INV-1", total_amount_paise=42_500_00,
            legs=[(30_000_00, date(2026, 9, 5)), (12_500_00, date(2026, 9, 20))], today=TODAY,
        )
        assert [leg.amount_paise for leg in plan.legs] == [30_000_00, 12_500_00]

    def test_legs_that_do_not_sum_to_the_invoice_are_refused(self):
        """Not repaired silently -- a shortfall is a real disagreement about
        what is owed, not a rounding problem."""
        with pytest.raises(PlanRejected) as exc:
            build_plan(invoice_id="INV-1", total_amount_paise=42_500_00,
                       legs=[(20_000_00, date(2026, 9, 5))], today=TODAY)
        assert "disagreement" in str(exc.value)

    def test_an_empty_or_non_positive_plan_is_refused(self):
        with pytest.raises(PlanRejected):
            build_plan(invoice_id="INV-1", total_amount_paise=42_500_00, legs=[], today=TODAY)
        with pytest.raises(PlanRejected):
            build_plan(invoice_id="INV-1", total_amount_paise=1_000_00,
                       legs=[(2_000_00, date(2026, 9, 5)), (-1_000_00, date(2026, 9, 6))], today=TODAY)


class TestInstrumentFollowsTheSplit:
    def test_two_large_legs_need_afa_on_every_debit(self):
        plan = build_plan(invoice_id="INV-1", total_amount_paise=42_500_00, legs=_two_legs(), today=TODAY)
        assert plan.instrument.instrument is InstrumentType.RECURRING_EMANDATE_AFA_PER_DEBIT
        assert plan.requires_afa_per_debit is True

    def test_splitting_further_drops_under_the_afa_ceiling(self):
        """The AFA-free ceiling is per debit, not per plan -- so the same
        total in smaller legs removes per-debit authentication entirely.
        This is the real reason to offer a longer split, and it comes from
        the existing instrument rules rather than from this module."""
        plan = build_plan(
            invoice_id="INV-1", total_amount_paise=42_500_00,
            legs=[(10_625_00, date(2026, 9, 5)), (10_625_00, date(2026, 9, 12)),
                  (10_625_00, date(2026, 9, 19)), (10_625_00, date(2026, 9, 26))],
            today=TODAY,
        )
        assert plan.instrument.instrument is InstrumentType.RECURRING_EMANDATE
        assert plan.requires_afa_per_debit is False

    def test_a_disputed_amount_never_gets_a_mandate(self):
        """§12.2 and NO_MANDATE_ON_DISPUTE -- routed away from every
        mandate-shaped instrument before the ceiling is even considered."""
        plan = build_plan(
            invoice_id="INV-1", total_amount_paise=42_500_00, legs=_two_legs(),
            disputed_paise=30_000_00, today=TODAY,
        )
        assert plan.instrument.instrument is InstrumentType.PAYMENT_LINK_UNDISPUTED_PORTION


class TestPricing:
    def test_a_leg_inside_the_window_is_discounted_at_the_published_rate(self):
        plan = build_plan(invoice_id="INV-1", total_amount_paise=42_500_00, legs=_two_legs(), today=TODAY)
        first = plan.legs[0]
        assert first.discounted_amount_paise == 20_825_00  # 2% of 21,250
        assert first.savings_paise == 425_00

    def test_a_leg_outside_the_window_is_priced_at_face_value(self):
        plan = build_plan(invoice_id="INV-1", total_amount_paise=42_500_00, legs=_two_legs(), today=TODAY)
        second = plan.legs[1]
        assert second.discounted_amount_paise is None
        assert second.savings_paise == 0
        assert second.payable_paise == second.amount_paise

    def test_totals_reflect_only_earned_discounts(self):
        plan = build_plan(invoice_id="INV-1", total_amount_paise=42_500_00, legs=_two_legs(), today=TODAY)
        assert plan.total_savings_paise == 425_00
        assert plan.total_payable_paise == 42_500_00 - 425_00

    def test_no_discount_is_invented_when_every_leg_is_late(self):
        plan = build_plan(
            invoice_id="INV-1", total_amount_paise=42_500_00,
            legs=[(21_250_00, date(2026, 12, 1)), (21_250_00, date(2026, 12, 20))], today=TODAY,
        )
        assert plan.total_savings_paise == 0
        assert plan.total_payable_paise == 42_500_00


class TestDescription:
    def test_the_summary_states_the_afa_consequence_when_it_is_avoided(self):
        plan = build_plan(
            invoice_id="INV-1", total_amount_paise=42_500_00,
            legs=[(10_625_00, date(2026, 9, 5)), (10_625_00, date(2026, 9, 12)),
                  (10_625_00, date(2026, 9, 19)), (10_625_00, date(2026, 9, 26))],
            today=TODAY,
        )
        text = describe_plan(plan)
        assert "one authorization covers the whole plan" in text

    def test_the_summary_never_states_a_consequence_or_fee(self):
        """Pricing here is arithmetic, not persuasion -- an invented late
        fee is exactly what the composer's own prompt forbids."""
        plan = build_plan(invoice_id="INV-1", total_amount_paise=42_500_00, legs=_two_legs(), today=TODAY)
        text = describe_plan(plan).lower()
        for forbidden in ("legal", "penalty", "late fee", "credit score", "collections"):
            assert forbidden not in text
