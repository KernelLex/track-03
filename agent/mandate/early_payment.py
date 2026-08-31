"""Early-payment discount — the B2B-side complement to the MSMED late-fee
calculation (agent/statutory/msmed.py). Where that module penalizes a
buyer for paying *late* (a statutory obligation, Section 16), this module
gives Family C (liquidity/willingness, DEVDOC_v6 §11.2) a genuinely new
lever: a voluntary commercial incentive for paying *early*. It changes the
amount a `create_payment_link` targets, not the action itself — Family C
already unlocks `create_payment_link` (§11.2's own table), so this is a
new *offer*, not a new rail action or a new ActionType.

Purely a commercial policy choice, not a statutory one — no MSMED/RBI/TRAI
clause requires or governs an early-payment discount, so this lives beside
agent/mandate/instrument.py (a similar "which terms to offer" decision),
not in agent/statutory/.

**The same honesty DEVDOC_v6 §17.2 insists on for `lift_prior` applies
here in full.** Neither the discount rate (2%) nor the window (10 days)
has a fitted or public source for Indian B2B AR specifically — 2%/10 days
is the textbook US trade-credit convention ("2/10 net 30"), used as a
declared, swept-in-spirit starting point, not a claim of being optimized
for this population. More importantly: **`EARLY_PAYMENT_LIFT` defaults to
1.0 — no assumed behavioural uplift from offering a discount** — because
assuming a discount converts better than chasing the full amount, with no
evidence either way, would be exactly the "authored your own result"
failure §17.1 warns about. At a lift of 1.0, offering a discount is
correctly, honestly EV-negative versus chasing the full amount (you gave
away money for the identical probability) — the discount only becomes the
better choice once a caller supplies a `Prior[float]` lift greater than
what makes up for the discount rate, the same swept-parameter discipline
`lift_prior` itself already follows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from agent.decide.ev import Decision, Prior, compute_ev

DEFAULT_DISCOUNT_RATE = 0.02
"""2% — the "2" in the classic "2/10 net 30" trade-credit convention. A
declared default, not a fitted one; see module docstring."""

DEFAULT_WINDOW_DAYS = 10
"""The "10" in "2/10 net 30" — days from the offer within which the
discount applies. Declared, not fitted; see module docstring."""

DEFAULT_EARLY_PAYMENT_LIFT: "Prior[float]" = Prior(1.0)
"""No assumed uplift — see module docstring. A caller with real evidence
of a discount converting better (or worse) than chasing the full amount
overrides this; the default deliberately does not assume the answer."""


class InvalidDiscountTerms(Exception):
    """A discount rate or window outside a sane range — never silently
    clamped, since a caller passing e.g. discount_rate=1.5 almost
    certainly made an arithmetic error upstream, not a deliberate choice."""


@dataclass(frozen=True, slots=True)
class EarlyPaymentOffer:
    invoice_id: str
    original_amount_paise: int
    discount_rate: float
    discounted_amount_paise: int
    savings_paise: int
    discount_valid_until: date

    def is_valid(self, *, as_of: date) -> bool:
        return as_of <= self.discount_valid_until


def compute_early_payment_offer(
    *,
    invoice_id: str,
    amount_paise: int,
    offer_date: date,
    discount_rate: float = DEFAULT_DISCOUNT_RATE,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> EarlyPaymentOffer:
    """Pure arithmetic, no model involved — the same Law 1 discipline every
    other amount-producing function in this codebase follows.
    `amount_paise` must already be the correct base (a real Invoice or
    InvoiceCtx amount), never a MODEL-extracted figure (Law 5)."""
    if amount_paise <= 0:
        raise ValueError("amount_paise must be positive")
    if not (0.0 <= discount_rate < 1.0):
        raise InvalidDiscountTerms(f"discount_rate must be in [0, 1), got {discount_rate}")
    if window_days <= 0:
        raise InvalidDiscountTerms(f"window_days must be positive, got {window_days}")

    savings_paise = round(amount_paise * discount_rate)
    return EarlyPaymentOffer(
        invoice_id=invoice_id,
        original_amount_paise=amount_paise,
        discount_rate=discount_rate,
        discounted_amount_paise=amount_paise - savings_paise,
        savings_paise=savings_paise,
        discount_valid_until=offer_date + timedelta(days=window_days),
    )


@dataclass(frozen=True, slots=True)
class EarlyPaymentEvComparison:
    full_price_decision: Decision
    discounted_decision: Decision
    discount_is_better: bool
    """True iff the discounted path's EV strictly exceeds the full-price
    path's EV. Ties favour the full-price path (no reason to give away
    money for an identical expected outcome)."""


def compare_full_price_vs_discount_ev(
    *,
    p_base: float,
    chase_lift_prior: "Prior[float]",
    full_amount_paise: int,
    discounted_amount_paise: int,
    cost_paise: int,
    early_payment_lift: "Prior[float]" = DEFAULT_EARLY_PAYMENT_LIFT,
) -> EarlyPaymentEvComparison:
    """Reuses agent.decide.ev.compute_ev() twice — once per path — rather
    than a new EV formula, so both paths are gated by the identical
    arithmetic and identical EV_FLOOR bounds rule downstream. `p_base` (the
    fitted probability this invoice pays by T absent intervention) is the
    same for both paths on purpose: the discount's effect on outcome is
    modelled entirely through `early_payment_lift`, not by silently also
    perturbing the fitted half of the EV formula."""
    full_price_decision = compute_ev(
        p_base=p_base, lift_prior=chase_lift_prior, recoverable_paise=full_amount_paise,
        cost_paise=cost_paise, action_type="create_payment_link",
    )
    discounted_decision = compute_ev(
        p_base=p_base, lift_prior=early_payment_lift, recoverable_paise=discounted_amount_paise,
        cost_paise=cost_paise, action_type="create_payment_link",
    )
    return EarlyPaymentEvComparison(
        full_price_decision=full_price_decision,
        discounted_decision=discounted_decision,
        discount_is_better=discounted_decision.ev_paise > full_price_decision.ev_paise,
    )
