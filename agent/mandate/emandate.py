"""Turn an agreed payment plan into real, authorizable e-mandate links.

`payment_plan.build_plan()` computes what a debtor owes and when. This is
the step that makes that actionable: a URL they can open and authorize, so
the instalments they agreed to actually debit themselves on the dates they
named. Without it the negotiation ends in a polite sentence and nothing
happens -- which is the failure mode this whole project argues against.

**Why one subscription per leg, not one per plan.** Razorpay's only
recurring primitive on this account is Plan + Subscription, and a plan
carries a *fixed per-cycle amount* (see `RazorpayRail.create_mandate`).
A real negotiated split is rarely even -- "21,000 on the 5th and the rest
on the 20th" is Rs 21,000 then Rs 21,500 -- and a single fixed-amount
subscription cannot express two different amounts. The options were to
round the legs into an even division (misrepresenting what the debtor
agreed to), to authorize at the larger amount and over-collect on the
smaller leg (taking money they did not agree to), or to issue one mandate
per distinct amount. Only the last one is honest, so that is what this
does. When the legs *are* equal -- which includes the common single-leg
case, "I'll pay the whole thing on the 5th" -- they collapse into one
subscription and the debtor authorizes once.

**Nothing here charges anyone.** A subscription in `created` state is an
authorization request. The debtor has to open the link and complete
authentication before a single rupee moves, and this module never calls a
charge API at all. That distinction is why creating these links on a
*proposal* is safe: it hands them the means to say yes, not a debit.

**A known divergence, stated rather than hidden.** `select_instrument()`
picks the instrument a plan *should* use, and for a single Rs 42,500 leg
that is `upi_block_reserve_pay`, not an e-mandate. This module issues a
mandate anyway, because a mandate is the only primitive this account has
that can debit on a *future date the debtor named* -- a UPI block is
created at pay time, and there is no rail call here that schedules one. So
the instrument decision is a recommendation this module cannot always
honour, and `PaymentPlan.instrument` is still reported truthfully
alongside the mandate rather than being rewritten to match it. Closing
this properly means a UPI Autopay path (`docs/RAIL_CAPABILITIES.md` lists
it as blocked: it needs explicit account approval), not pretending the
recommendation and the artifact agree.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from agent.mandate.payment_plan import PaymentPlan, PlanLeg
from agent.rails.types import MandateSpec

_log = logging.getLogger("trucommit.emandate")

MIN_LEAD_TIME = timedelta(minutes=30)
"""Razorpay refuses a `start_at` that isn't comfortably in the future, and
a debtor who says "today" or "tomorrow morning" is proposing a date that
can land inside that margin. A leg due sooner than this is created without
a scheduled start -- the mandate still authorizes, the first debit just
falls on authorization instead of failing to exist at all."""


@dataclass(frozen=True, slots=True)
class MandateLink:
    """One real, authorizable mandate covering one or more plan legs."""

    mandate_id: str
    short_url: str
    amount_paise: int
    """The per-debit amount, not the plan total."""
    sequences: tuple[int, ...]
    """Which plan legs this mandate covers, by `PlanLeg.sequence`."""
    first_debit_on: date
    afa_required: bool

    @property
    def covers_whole_plan(self) -> bool:
        return len(self.sequences) > 1


class MandateCreationFailed(Exception):
    """The rail didn't produce an authorizable mandate.

    Raised rather than returning a placeholder URL. A link that looks real
    and isn't is strictly worse than no link -- this project shipped one
    once (`https://rzp.io/i/pending`, in a real message to a real person)
    and the rule since then is that an unavailable link means the message
    says so plainly."""


def _group_legs(legs: tuple[PlanLeg, ...]) -> list[tuple[int, list[PlanLeg]]]:
    """Legs grouped by payable amount, since that is exactly what one
    subscription can cover. Order follows the first occurrence, so the
    earliest instalment is always the first link the debtor sees."""
    groups: dict[int, list[PlanLeg]] = {}
    for leg in legs:
        groups.setdefault(leg.payable_paise, []).append(leg)
    return sorted(groups.items(), key=lambda item: min(leg.sequence for leg in item[1]))


def _start_at(due: date, *, now: datetime | None = None) -> str:
    """A leg's due date as an ISO instant the rail can schedule against.

    Midday UTC rather than midnight: a mandate scheduled for 00:00 on the
    5th is, in IST, the evening of the 4th -- debiting a debtor a day
    before the date they actually named. Legs too close to now to schedule
    return "" and are created without a start date (see MIN_LEAD_TIME)."""
    now = now or datetime.now(timezone.utc)
    instant = datetime.combine(due, time(12, 0), tzinfo=timezone.utc)
    return instant.isoformat() if instant - now >= MIN_LEAD_TIME else ""


def create_plan_mandates(
    plan: PaymentPlan, *, rail, customer_contact: str | None = None, now: datetime | None = None,
) -> list[MandateLink]:
    """Real mandates for `plan`, one per distinct instalment amount.

    Raises MandateCreationFailed if the rail refuses -- partial success is
    not returned quietly, because a debtor handed one of two links would
    reasonably believe the whole plan was set up.
    """
    if not plan.legs:
        raise MandateCreationFailed("a plan with no instalments has nothing to authorize")

    links: list[MandateLink] = []
    for amount_paise, legs in _group_legs(plan.legs):
        due_dates = sorted(leg.due_date for leg in legs)
        spec = MandateSpec(
            max_amount_paise=amount_paise,
            start_at=_start_at(due_dates[0], now=now),
            end_at=due_dates[-1].isoformat(),
            debit_schedule=[d.isoformat() for d in due_dates],
            afa_required=plan.requires_afa_per_debit,
            customer_contact=customer_contact,
        )
        try:
            mandate = rail.create_mandate(spec)
        except Exception as exc:  # rail-specific errors vary; all mean "no mandate"
            raise MandateCreationFailed(
                f"the rail refused a mandate for Rs {amount_paise / 100:,.0f}: {exc}"
            ) from exc

        if not mandate.short_url:
            # A mandate with no authorization URL is unauthorizable, so it
            # is useless to the debtor however healthy it looks server-side.
            raise MandateCreationFailed(f"mandate {mandate.id} came back with no authorization URL")

        links.append(MandateLink(
            mandate_id=mandate.id,
            short_url=mandate.short_url,
            amount_paise=amount_paise,
            sequences=tuple(leg.sequence for leg in sorted(legs, key=lambda l: l.sequence)),
            first_debit_on=due_dates[0],
            afa_required=plan.requires_afa_per_debit,
        ))
    return links


def describe_mandate_links(links: list[MandateLink]) -> str:
    """Plain-language lines naming each link and what authorizing it does.

    Written for the message composer's context block, so it states only
    what is true of a `created` subscription: authorizing sets up the
    debit, it does not take the money now."""
    if not links:
        return ""
    lines = []
    for link in links:
        legs = "instalment " + " and ".join(str(s) for s in link.sequences)
        lines.append(
            f"  {legs}: Rs {link.amount_paise / 100:,.0f} on {link.first_debit_on.isoformat()}"
            + (f" (x{len(link.sequences)})" if link.covers_whole_plan else "")
            + f" -- authorize at {link.short_url}"
        )
    header = (
        "Live e-mandate link, ready to authorize:" if len(links) == 1
        else f"Live e-mandate links ({len(links)}, one per instalment amount -- "
             "the amounts differ, so each is authorized separately):"
    )
    footer = ("  Authorizing sets up the debit on that date. It does not charge anything now.")
    return "\n".join([header, *lines, footer])
