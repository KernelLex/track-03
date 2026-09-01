"""Turning an agreed plan into real, authorizable e-mandate links.

The behaviour under test is mostly about honesty: a link that looks real
and isn't is worse than no link (this project shipped one once, to a real
person), and a mandate whose amount doesn't match what the debtor agreed
to would take money they never consented to. Both are asserted here.

The rail is a stand-in throughout -- no network, no real Razorpay objects.
The live counterpart is tests/agent/test_razorpay_rail_live.py.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from agent.mandate.emandate import (
    MandateCreationFailed,
    create_plan_mandates,
    describe_mandate_links,
)
from agent.mandate.payment_plan import build_plan
from agent.rails.types import Mandate

TODAY = date(2026, 9, 1)
NOW = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


class _FakeRail:
    """Records what it was asked for, so the assertions can be about the
    request rather than only about the returned object."""

    def __init__(self, *, fail: bool = False, short_url: str | None = "https://rzp.io/rzp/REAL"):
        self.specs = []
        self.fail = fail
        self.short_url = short_url
        self._n = 0

    def create_mandate(self, spec):
        if self.fail:
            raise RuntimeError("rail said no")
        self.specs.append(spec)
        self._n += 1
        return Mandate(
            id=f"sub_{self._n}", rail="razorpay", max_amount_paise=spec.max_amount_paise,
            start_at=spec.start_at, end_at=spec.end_at, status="created",
            afa_required=spec.afa_required, debit_schedule=list(spec.debit_schedule),
            short_url=self.short_url,
        )


def _plan(legs, total=42_500_00):
    return build_plan(invoice_id="INV-2201", total_amount_paise=total, legs=legs, today=TODAY)


class TestEqualLegsShareOneMandate:
    """One authorization, not several, whenever the rail can express it."""

    def test_a_single_full_payment_makes_exactly_one_link(self):
        """"I'll pay the whole thing on the 5th" -- the plain case, and the
        one a debtor is most likely to accept."""
        plan = _plan([(42_500_00, date(2026, 9, 5))])
        links = create_plan_mandates(plan, rail=_FakeRail(), now=NOW)

        assert len(links) == 1
        assert links[0].sequences == (1,)
        assert links[0].short_url == "https://rzp.io/rzp/REAL"
        # The priced amount, not the face amount -- this leg falls inside
        # the early-payment window, and the mandate must debit what the
        # debtor was actually quoted.
        assert links[0].amount_paise == plan.legs[0].payable_paise
        assert links[0].amount_paise < 42_500_00

    def test_two_equal_legs_collapse_into_one_authorization(self):
        """Both outside the early-payment window, so both price at face
        value -- which is what lets one fixed-amount subscription cover
        them."""
        plan = _plan([(21_250_00, date(2026, 10, 5)), (21_250_00, date(2026, 10, 19))])
        rail = _FakeRail()
        links = create_plan_mandates(plan, rail=rail, now=NOW)

        assert len(links) == 1
        assert links[0].sequences == (1, 2)
        assert links[0].covers_whole_plan is True
        # One subscription, two scheduled debits -- which is what makes it
        # a single authorization rather than two.
        assert rail.specs[0].debit_schedule == ["2026-10-05", "2026-10-19"]


    def test_a_discount_on_only_one_leg_correctly_splits_them(self):
        """Equal face amounts stop being equal once one leg earns the
        early-payment discount and the other doesn't. Grouping on the face
        value would authorize the discounted leg at the undiscounted
        price -- charging away the discount that was just offered."""
        plan = _plan([(21_250_00, date(2026, 9, 5)), (21_250_00, date(2026, 10, 19))])
        links = create_plan_mandates(plan, rail=_FakeRail(), now=NOW)

        assert len(links) == 2
        assert links[0].amount_paise < links[1].amount_paise
        assert links[1].amount_paise == 21_250_00


class TestUnequalLegsGetSeparateMandates:
    """The rail carries a fixed per-cycle amount, so unequal legs genuinely
    cannot share one subscription. What matters is which way that resolves."""

    def test_each_distinct_amount_gets_its_own_link(self):
        plan = _plan([(21_000_00, date(2026, 9, 5)), (21_500_00, date(2026, 9, 19))])
        links = create_plan_mandates(plan, rail=_FakeRail(), now=NOW)

        assert [link.amount_paise for link in links] == [leg.payable_paise for leg in plan.legs]
        assert [link.sequences for link in links] == [(1,), (2,)]

    def test_no_leg_is_ever_authorized_for_more_than_it_is_worth(self):
        """The tempting shortcut is one mandate at the larger amount. That
        would debit Rs 21,500 for a leg the debtor agreed to at Rs 21,000."""
        plan = _plan([(21_000_00, date(2026, 9, 5)), (21_500_00, date(2026, 9, 19))])
        rail = _FakeRail()
        create_plan_mandates(plan, rail=rail, now=NOW)

        authorized = sorted(spec.max_amount_paise for spec in rail.specs)
        assert authorized == sorted(leg.payable_paise for leg in plan.legs)
        # Each debit is the leg's own priced amount -- never the larger leg's,
        # and never more than the face value of the instalment it covers.
        for spec, leg in zip(rail.specs, plan.legs):
            assert spec.max_amount_paise <= leg.amount_paise

    def test_the_earliest_instalment_is_the_first_link_offered(self):
        """A debtor reading the reply should meet the thing due soonest
        first, whatever order the amounts happened to group in."""
        plan = _plan([(20_000_00, date(2026, 9, 5)), (22_500_00, date(2026, 9, 19))])
        links = create_plan_mandates(plan, rail=_FakeRail(), now=NOW)
        assert links[0].first_debit_on == date(2026, 9, 5)


class TestTheDebtorsOwnDateIsHonoured:
    def test_the_mandate_is_scheduled_for_the_date_they_named(self):
        plan = _plan([(42_500_00, date(2026, 9, 5))])
        rail = _FakeRail()
        create_plan_mandates(plan, rail=rail, now=NOW)
        assert rail.specs[0].start_at.startswith("2026-09-05")

    def test_it_is_scheduled_at_midday_not_midnight(self):
        """Midnight UTC on the 5th is the evening of the 4th in IST -- a day
        earlier than the debtor agreed to."""
        plan = _plan([(42_500_00, date(2026, 9, 5))])
        rail = _FakeRail()
        create_plan_mandates(plan, rail=rail, now=NOW)
        assert "T12:00" in rail.specs[0].start_at

    def test_a_date_too_close_to_now_is_created_without_a_start_date(self):
        """Razorpay refuses a start_at that isn't comfortably ahead. A
        mandate they can still authorize beats a hard failure."""
        plan = _plan([(42_500_00, date(2026, 9, 1))])
        rail = _FakeRail()
        links = create_plan_mandates(plan, rail=rail, now=datetime(2026, 9, 1, 11, 55, tzinfo=timezone.utc))

        assert rail.specs[0].start_at == ""
        assert links[0].short_url  # still authorizable


class TestFailuresAreNeverPaperedOver:
    """The rule after shipping `https://rzp.io/i/pending` to a real person:
    an unavailable link means saying so, never producing something that
    looks like one."""

    def test_a_rail_refusal_raises_rather_than_returning_a_placeholder(self):
        plan = _plan([(42_500_00, date(2026, 9, 5))])
        with pytest.raises(MandateCreationFailed, match="refused a mandate"):
            create_plan_mandates(plan, rail=_FakeRail(fail=True), now=NOW)

    def test_a_mandate_with_no_authorization_url_is_treated_as_a_failure(self):
        """It may be a perfectly healthy object server-side and still be
        useless to the debtor, who has no way to authorize it."""
        plan = _plan([(42_500_00, date(2026, 9, 5))])
        with pytest.raises(MandateCreationFailed, match="no authorization URL"):
            create_plan_mandates(plan, rail=_FakeRail(short_url=None), now=NOW)

    def test_a_partial_failure_is_not_returned_as_success(self):
        """A debtor handed one of two links would reasonably believe the
        whole plan was set up."""
        class _FailsOnSecond(_FakeRail):
            def create_mandate(self, spec):
                if len(self.specs) >= 1:
                    raise RuntimeError("second one failed")
                return super().create_mandate(spec)

        plan = _plan([(21_000_00, date(2026, 9, 5)), (21_500_00, date(2026, 9, 19))])
        with pytest.raises(MandateCreationFailed):
            create_plan_mandates(plan, rail=_FailsOnSecond(), now=NOW)


class TestTheDescriptionGivenToTheComposer:
    def test_it_names_the_amount_the_date_and_the_real_url(self):
        plan = _plan([(42_500_00, date(2026, 10, 5))])
        text = describe_mandate_links(create_plan_mandates(plan, rail=_FakeRail(), now=NOW))

        assert "42,500" in text
        assert "2026-10-05" in text
        assert "https://rzp.io/rzp/REAL" in text

    def test_it_says_authorizing_does_not_charge_anything_now(self):
        """A `created` subscription is an authorization request. Letting the
        composer imply the money has moved would be a false confirmation --
        the one thing its prompt forbids hardest."""
        plan = _plan([(42_500_00, date(2026, 9, 5))])
        text = describe_mandate_links(create_plan_mandates(plan, rail=_FakeRail(), now=NOW))
        assert "does not charge anything now" in text

    def test_separate_links_explain_why_there_is_more_than_one(self):
        plan = _plan([(21_000_00, date(2026, 9, 5)), (21_500_00, date(2026, 9, 19))])
        text = describe_mandate_links(create_plan_mandates(plan, rail=_FakeRail(), now=NOW))
        assert "one per instalment amount" in text

    def test_no_links_describes_as_nothing_rather_than_an_empty_heading(self):
        assert describe_mandate_links([]) == ""
