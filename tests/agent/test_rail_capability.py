"""What the spec recommends vs. what this account can actually issue.

The gap this closes was real and was documented as a limitation before it
was fixed: the demo reported `upi_block_reserve_pay` -- the correct §12.2
answer -- while quietly issuing an e-mandate, because UPI Autopay is not
approved on this Razorpay account. Reporting an instrument you are not
creating is the kind of small dishonesty that makes everything else in a
demo suspect.

Pure functions, no rail, no network.
"""

from __future__ import annotations

from datetime import date

from agent.mandate.instrument import (
    AFA_FREE_CEILING_PAISE,
    InstrumentType,
    Promise,
    select_instrument,
)
from agent.mandate.payment_plan import build_plan, describe_plan
from agent.mandate.rail_capability import (
    UNAVAILABLE_ON_THIS_ACCOUNT,
    deployable_instrument,
    describe_deployment,
)

TODAY = date(2026, 9, 1)


def _deployed(total_paise, *, installments=1, installment_amount_paise=None):
    return deployable_instrument(select_instrument(Promise(
        total_amount_paise=total_paise, installments=installments,
        installment_amount_paise=installment_amount_paise,
    )))


class TestTheSpecTableIsNotRewritten:
    """§12.2 is a decision table. One that bends to whichever account is
    configured is not a decision table, so the substitution happens on top
    of `select_instrument()`, never inside it."""

    def test_select_instrument_still_answers_upi_for_a_single_large_payment(self):
        spec = select_instrument(Promise(total_amount_paise=42_500_00))
        assert spec.instrument == InstrumentType.UPI_BLOCK_RESERVE_PAY

    def test_the_recommendation_is_preserved_on_the_substitution(self):
        deployed = _deployed(42_500_00)
        assert deployed.recommended == InstrumentType.UPI_BLOCK_RESERVE_PAY
        assert deployed.substituted is True

    def test_the_reason_names_the_account_not_the_instrument(self):
        """The instrument is a correct answer. The account is what can't
        issue it -- and on an approved account nothing here would change."""
        assert "not approved on this Razorpay account" in _deployed(42_500_00).reason


class TestUpiBecomesANetbankingEmandate:
    def test_a_large_single_payment_deploys_as_an_emandate(self):
        deployed = _deployed(42_500_00)
        assert deployed.deployable == InstrumentType.RECURRING_EMANDATE_AFA_PER_DEBIT
        assert deployed.is_emandate is True

    def test_a_small_single_payment_deploys_without_per_debit_afa(self):
        deployed = _deployed(9_000_00)
        assert deployed.recommended == InstrumentType.UPI_AUTOPAY_ONE_TIME
        assert deployed.deployable == InstrumentType.RECURRING_EMANDATE
        assert deployed.requires_afa is False

    def test_the_afa_ceiling_survives_the_substitution(self):
        """The property worth preserving: over Rs 15,000 needs
        authentication whichever instrument carries it."""
        assert _deployed(AFA_FREE_CEILING_PAISE).requires_afa is False
        assert _deployed(AFA_FREE_CEILING_PAISE + 1).requires_afa is True


class TestAvailableInstrumentsPassThroughUntouched:
    """The common case must be indistinguishable from having no capability
    layer at all."""

    def test_an_emandate_recommendation_is_not_substituted(self):
        deployed = _deployed(42_500_00, installments=2, installment_amount_paise=21_250_00)
        assert deployed.substituted is False
        assert deployed.deployable == deployed.recommended

    def test_a_payment_link_recommendation_is_not_substituted(self):
        spec = select_instrument(Promise(total_amount_paise=42_500_00, declined=True))
        deployed = deployable_instrument(spec)
        assert deployed.substituted is False
        assert deployed.deployable == InstrumentType.PAYMENT_LINK_PLUS_REMINDER

    def test_an_untouched_recommendation_keeps_its_original_rationale(self):
        spec = select_instrument(Promise(total_amount_paise=42_500_00, declined=True))
        assert deployable_instrument(spec).reason == spec.rationale

    def test_no_emandate_or_link_instrument_is_listed_as_unavailable(self):
        """A guard on the unavailability set itself -- adding an e-mandate
        to it would make every substitution route to something the rail
        equally cannot issue."""
        assert UNAVAILABLE_ON_THIS_ACCOUNT == {
            InstrumentType.UPI_AUTOPAY_ONE_TIME,
            InstrumentType.UPI_BLOCK_RESERVE_PAY,
        }


class TestThePlanReportsBoth:
    def test_a_single_leg_plan_reports_the_emandate_it_actually_issues(self):
        plan = build_plan(invoice_id="INV-2201", total_amount_paise=42_500_00,
                          legs=[(42_500_00, date(2026, 10, 5))], today=TODAY)
        assert plan.deployment.deployable == InstrumentType.RECURRING_EMANDATE_AFA_PER_DEBIT
        assert plan.requires_afa_per_debit is True

    def test_the_summary_names_the_substitution_rather_than_hiding_it(self):
        plan = build_plan(invoice_id="INV-2201", total_amount_paise=42_500_00,
                          legs=[(42_500_00, date(2026, 10, 5))], today=TODAY)
        summary = describe_plan(plan)

        assert "recurring_emandate_afa_per_debit" in summary
        assert "12.2 recommends upi_block_reserve_pay" in summary

    def test_a_split_plan_summary_says_nothing_about_substitution(self):
        """Nothing was substituted, so there is nothing to explain."""
        plan = build_plan(invoice_id="INV-2201", total_amount_paise=42_500_00,
                          legs=[(21_250_00, date(2026, 10, 5)), (21_250_00, date(2026, 10, 19))],
                          today=TODAY)
        assert "recommends" not in describe_plan(plan)


class TestTheOneLineDescription:
    def test_it_names_both_when_substituted(self):
        text = describe_deployment(_deployed(42_500_00))
        assert "recurring_emandate_afa_per_debit" in text
        assert "recommended upi_block_reserve_pay" in text

    def test_it_names_only_the_instrument_when_nothing_changed(self):
        text = describe_deployment(_deployed(42_500_00, installments=2,
                                             installment_amount_paise=21_250_00))
        assert text == "Instrument: recurring_emandate_afa_per_debit"
