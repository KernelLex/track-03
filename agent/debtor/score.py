"""What a debtor's own track record earns them.

`BoundsContext.debtor.promise_credibility` has been in this system since
the bounds gate was written, and `PROMISE_COOLDOWN` already scales its
grace period by it (`grace_days * promise_credibility`, in both
`rules.yaml` and `human_twin.py`). The rule's own comment says the value is
"computed upstream ... from captured payments". Nothing computed it. Every
context in the codebase used the `1.0` default, so a debtor who had broken
four promises got exactly the same quiet time as one who had never broken
any. This module is the missing upstream half.

**The score is arithmetic over settled facts, never a judgement of
sincerity.** It is kept-over-resolved across the trailing five promises,
where "kept" means a rail-confirmed capture arrived (Law 7's standard, the
same one `RecoveryLedger.attribute()` enforces) and "broken" means the
promised date passed without one. A model's read of how sincere a message
sounded never enters it -- that is SYSTEM provenance by construction, and
it is what makes the score defensible to the person it is applied to.

**What the score is allowed to decide, and what it is not.** It decides
things this business may legitimately choose: how long to wait before
chasing again, how many instalments to offer, whether to offer a plan at
all, and whether to press a statutory interest claim now or hold it.

It does **not** decide what is owed. The MSMED statutory interest rate is
set by law and computed by `agent/statutory/msmed.py`; a score cannot
raise it, and this module never invents a late fee of its own -- the same
prohibition `compose.py`'s prompt and `payment_plan.py` already carry. A
"late fee that gets worse if your score is bad" would be exactly the
invented penalty this project refuses to produce.

The early-payment discount does vary by band, and that is a different
thing: it is a voluntary commercial offer, published as a fixed rate per
band here rather than negotiated per debtor, so two debtors in the same
band are offered the same terms.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from agent.mandate.early_payment import DEFAULT_DISCOUNT_RATE

CREDIBILITY_WINDOW = 5
"""Trailing promises considered. Matches `rules.yaml`'s own stated
definition ("over the trailing 5 promises") rather than introducing a
second, different window for the same concept."""

NO_HISTORY_CREDIBILITY = 1.0
"""A debtor with no resolved promises gets the benefit of the doubt.

The alternative -- starting everyone at zero -- would apply the strictest
terms to exactly the people this system knows least about, which is both
unfair and self-defeating: a first-time debtor would be refused the
instalment plan most likely to get them to pay."""


@dataclass(frozen=True, slots=True)
class PromiseOutcome:
    """One promise, and whether it was kept. `outcome` is 'kept', 'broken',
    or 'pending' -- pending promises are not yet evidence either way and
    are excluded from the score rather than counted as failures."""

    invoice_id: str
    promised_amount_paise: int
    promised_date: date
    outcome: str
    recorded_at: str = ""
    payment_id: str | None = None
    """The rail-confirmed capture that settled it, when kept. Present so a
    score can be audited back to the payment that justifies it."""


@dataclass(frozen=True, slots=True)
class DebtorTerms:
    """The terms this debtor's record earns. Every field is derived, and
    `rationale` says from what -- a debtor asking "why am I being offered
    this" deserves an answer that isn't 'the model decided'."""

    band: str
    credibility: float
    resolved_promises: int
    kept_promises: int
    grace_days: int
    max_instalments: int
    offers_instalment_plan: bool
    early_discount_rate: float
    press_statutory_interest: bool
    rationale: str

    @property
    def credibility_pct(self) -> int:
        return round(self.credibility * 100)


BANDS = (
    # (minimum credibility, band, grace_days, max_instalments, discount_rate, press_interest)
    (0.80, "trusted", 10, 4, DEFAULT_DISCOUNT_RATE, False),
    (0.50, "standard", 7, 3, DEFAULT_DISCOUNT_RATE, False),
    (0.25, "watch", 3, 2, DEFAULT_DISCOUNT_RATE / 2, False),
    (0.00, "strict", 1, 1, 0.0, True),
)
"""Published bands, in descending order of credibility.

Fixed per band rather than computed per debtor on purpose: a continuous
function of the score would mean every debtor is offered subtly different
terms, which is impossible to explain to any of them and impossible to
audit. Four bands can be written on a page.

`strict` is the only band that presses statutory interest and the only one
that offers no instalment plan -- a debtor who has broken three of their
last four promises has told you what a fourth one is worth. It still gets
a grace day and a payment route; it is not a cutoff.
"""


def promise_credibility(outcomes: list[PromiseOutcome]) -> float:
    """kept / resolved over the trailing `CREDIBILITY_WINDOW` promises.

    Pending promises are excluded, not counted as broken: a promise whose
    date has not yet arrived is not evidence of anything, and treating it
    as a failure would penalise a debtor the moment they made a commitment.
    """
    resolved = [o for o in outcomes if o.outcome in ("kept", "broken")][-CREDIBILITY_WINDOW:]
    if not resolved:
        return NO_HISTORY_CREDIBILITY
    kept = sum(1 for o in resolved if o.outcome == "kept")
    return kept / len(resolved)


def terms_for(outcomes: list[PromiseOutcome]) -> DebtorTerms:
    """The published terms this record earns."""
    credibility = promise_credibility(outcomes)
    resolved = [o for o in outcomes if o.outcome in ("kept", "broken")][-CREDIBILITY_WINDOW:]
    kept = sum(1 for o in resolved if o.outcome == "kept")

    for minimum, band, grace, instalments, discount, press in BANDS:
        if credibility >= minimum:
            break

    if not resolved:
        rationale = (
            "No settled promises on record yet, so the benefit of the doubt applies: "
            f"standard {band} terms. Applying the strictest terms to the debtors this "
            "system knows least about would be both unfair and self-defeating."
        )
    else:
        rationale = (
            f"{kept} of the last {len(resolved)} promise(s) kept "
            f"({round(credibility * 100)}%), from rail-confirmed captures only. "
            f"That places this debtor in the '{band}' band: up to {instalments} "
            f"instalment(s), {grace} day(s) of grace past a promised date, and "
            + (f"a {discount * 100:.0f}% early-payment discount."
               if discount else "no early-payment discount.")
        )

    return DebtorTerms(
        band=band, credibility=credibility,
        resolved_promises=len(resolved), kept_promises=kept,
        grace_days=grace, max_instalments=instalments,
        offers_instalment_plan=instalments > 1,
        early_discount_rate=discount,
        press_statutory_interest=press,
        rationale=rationale,
    )
