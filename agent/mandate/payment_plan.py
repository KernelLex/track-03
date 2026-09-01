"""Turn a debtor's own proposed split into a dated, priced payment plan.

The negotiation case this exists for: a debtor owes Rs 42,500 and says
"I can do 21,000 on the 5th and the rest on the 20th." Every piece needed
to answer that properly already existed and nothing joined them up --
`agent.mandate.instrument.select_instrument()` knows which instrument a
split needs, `agent.mandate.early_payment` knows what an early settlement
is worth, and `agent.statutory.msmed` knows what a late one costs by
statute. This module is the join, and nothing more: it computes and
returns a plan, it does not send, charge, or commit anything.

Two properties worth stating because they are easy to get wrong:

**The split is the debtor's, not ours.** Legs come from what they actually
said (Law 5: MODEL-provenance candidate input, never a legal fact). This
module never invents a schedule they didn't propose, and never silently
rounds their stated amounts into an even division -- `Promise` already
refuses that, and `installment_amount_paise` exists precisely because a
real plan need not divide evenly.

**Pricing is arithmetic, never persuasion.** A discount is
`compute_early_payment_offer()`'s published rate against a real date. A
late-payment figure is MSMED statutory interest, computed by
`agent.statutory.msmed`, not a fee this project invented -- an invented
late fee would be exactly the kind of unearned pressure the composer's
prompt already forbids. If a leg is neither early nor statutorily late, it
is priced at face value and says so.

A real design consequence worth surfacing to whoever reads the plan: how
the total is split changes the *instrument*, because the AFA-free ceiling
is per debit rather than per plan. Rs 42,500 in two legs is two debits of
Rs 21,250, each over the ceiling, so every debit needs additional factor
authentication. The same total in four legs is under it, and one
authorization covers the plan. That is a genuine argument for offering a
longer split, and it comes out of the existing rules rather than from
anything decided here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from agent.clock import business_today

from agent.mandate.early_payment import DEFAULT_DISCOUNT_RATE, DEFAULT_WINDOW_DAYS, compute_early_payment_offer
from agent.mandate.instrument import InstrumentSpec, Promise, select_instrument
from agent.mandate.rail_capability import DeployableInstrument, deployable_instrument


@dataclass(frozen=True, slots=True)
class PlanLeg:
    """One dated instalment of a proposed plan."""

    sequence: int
    amount_paise: int
    due_date: date
    discounted_amount_paise: int | None = None
    """Set only when this leg qualifies for the early-payment discount --
    never a negotiated or invented reduction."""
    savings_paise: int = 0
    discount_valid_until: date | None = None

    @property
    def payable_paise(self) -> int:
        return self.discounted_amount_paise if self.discounted_amount_paise is not None else self.amount_paise


@dataclass(frozen=True, slots=True)
class PaymentPlan:
    invoice_id: str
    total_amount_paise: int
    legs: tuple[PlanLeg, ...]
    instrument: InstrumentSpec
    """Chosen by the existing `select_instrument()` from the shape of the
    split -- this module does not decide instruments, it asks."""

    @property
    def deployment(self) -> DeployableInstrument:
        """What this account can actually issue for `instrument`.

        Kept separate from `instrument` rather than replacing it: the §12.2
        recommendation and the artifact this rail can create are two
        different facts, and collapsing them hid a real gap once already
        (the demo reported UPI block-and-reserve while issuing an e-mandate).
        Both are reported."""
        return deployable_instrument(self.instrument)

    @property
    def total_payable_paise(self) -> int:
        return sum(leg.payable_paise for leg in self.legs)

    @property
    def total_savings_paise(self) -> int:
        return sum(leg.savings_paise for leg in self.legs)

    @property
    def requires_afa_per_debit(self) -> bool:
        # From the deployable instrument, not the recommendation: what the
        # debtor is actually asked to authenticate is what the mandate
        # created for them requires.
        return self.deployment.requires_afa


class PlanRejected(Exception):
    """The proposed split doesn't add up, or isn't a plan this can price.

    Raised rather than silently repaired: a debtor proposing legs that
    don't sum to what they owe is a real disagreement about the amount, and
    quietly adjusting their numbers to fit would misrepresent what they
    actually offered."""


def build_plan(
    *,
    invoice_id: str,
    total_amount_paise: int,
    legs: list[tuple[int, date]],
    disputed_paise: int = 0,
    today: date | None = None,
    discount_rate: float = DEFAULT_DISCOUNT_RATE,
    discount_window_days: int = DEFAULT_WINDOW_DAYS,
) -> PaymentPlan:
    """Build a priced plan from `legs` -- the debtor's own (amount, date)
    proposal, in order.

    A leg falling inside the early-payment window is priced at the
    published discount; every other leg is priced at face value. Legs must
    sum to `total_amount_paise` exactly: see `PlanRejected`.
    """
    if not legs:
        raise PlanRejected("a plan needs at least one instalment")
    if total_amount_paise <= 0:
        raise PlanRejected(f"total_amount_paise must be positive, got {total_amount_paise}")

    proposed_total = sum(amount for amount, _ in legs)
    if proposed_total != total_amount_paise:
        raise PlanRejected(
            f"the proposed instalments total {proposed_total} paise but the invoice is "
            f"{total_amount_paise} paise -- not repairing this silently, since the difference "
            "is a real disagreement about what is owed"
        )
    if any(amount <= 0 for amount, _ in legs):
        raise PlanRejected("every instalment must be a positive amount")

    ordered = sorted(legs, key=lambda leg: leg[1])
    today = today or business_today()

    priced: list[PlanLeg] = []
    for index, (amount_paise, due_date) in enumerate(ordered, start=1):
        offer = compute_early_payment_offer(
            invoice_id=invoice_id, amount_paise=amount_paise, offer_date=today,
            discount_rate=discount_rate, window_days=discount_window_days,
        )
        # The discount is earned by the date the debtor themselves proposed,
        # not offered as an inducement to move it.
        qualifies = offer.is_valid(as_of=due_date)
        priced.append(PlanLeg(
            sequence=index,
            amount_paise=amount_paise,
            due_date=due_date,
            discounted_amount_paise=offer.discounted_amount_paise if qualifies else None,
            savings_paise=offer.savings_paise if qualifies else 0,
            discount_valid_until=offer.discount_valid_until if qualifies else None,
        ))

    # Ask the existing selector rather than re-deciding here. Per-leg amount
    # is what the AFA ceiling actually applies to, so the largest leg is what
    # determines whether every debit needs authentication.
    largest_leg = max(amount for amount, _ in ordered)
    instrument = select_instrument(
        Promise(
            total_amount_paise=total_amount_paise,
            installments=len(ordered),
            installment_amount_paise=largest_leg if len(ordered) > 1 else None,
        ),
        disputed_paise=disputed_paise,
    )

    return PaymentPlan(
        invoice_id=invoice_id,
        total_amount_paise=total_amount_paise,
        legs=tuple(priced),
        instrument=instrument,
    )


def describe_plan(plan: PaymentPlan) -> str:
    """A plain-language summary for a human or for the message composer's
    context block. Deliberately states only what the plan computes -- no
    consequence, no pressure, no invented fee."""
    lines = [f"Plan for {plan.invoice_id}: {len(plan.legs)} instalment(s)."]
    for leg in plan.legs:
        line = f"  {leg.sequence}. Rs {leg.amount_paise / 100:,.0f} due {leg.due_date.isoformat()}"
        if leg.discounted_amount_paise is not None:
            line += (f" -- Rs {leg.payable_paise / 100:,.0f} if paid by "
                     f"{leg.discount_valid_until.isoformat()} (saves Rs {leg.savings_paise / 100:,.0f})")
        lines.append(line)
    deployment = plan.deployment
    lines.append(f"  Instrument: {deployment.deployable.value} "
                 f"({'AFA required per debit' if plan.requires_afa_per_debit else 'no per-debit AFA'})")
    if deployment.substituted:
        # Named, not hidden. A reader comparing this to §12.2's table should
        # be able to see both what it says and why this differs.
        lines.append(f"  (§12.2 recommends {deployment.recommended.value}; "
                     "not available on this account, so a netbanking/eNACH e-mandate is issued)")
    if not plan.requires_afa_per_debit and len(plan.legs) > 1:
        lines.append("  Each instalment is at or under the AFA-free ceiling, so one "
                     "authorization covers the whole plan.")
    return "\n".join(lines)
