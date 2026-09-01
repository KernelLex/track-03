"""What the spec recommends vs. what this account can actually issue.

`select_instrument()` answers "which instrument *should* this promise
use", parametrized directly on DEVDOC_v6 §12.2's table. It is deliberately
rail-independent, and it must stay that way -- a decision table that
silently bends to whichever account happens to be configured is not a
decision table.

But a recommendation this rail cannot issue is not a plan. This account
has **no UPI Autopay approval** (`docs/RAIL_CAPABILITIES.md` records the
probe as blocked: it needs explicit enablement and a specific subscription
auth_type). So every UPI-shaped recommendation -- `upi_autopay_one_time`
and `upi_block_reserve_pay` -- is undeployable here, and pretending
otherwise produced exactly one bad outcome: the demo reported an
instrument it was not creating, while quietly issuing an e-mandate
instead (recorded in `docs/LIMITATIONS.md` before this module existed).

This module is the join, and it keeps both halves visible. The
recommendation is preserved and reported as the recommendation; the
substitution is reported as a substitution, with the reason attached.
Nothing is rewritten to look like it agreed all along.

**What the substitute actually is.** A netbanking/eNACH e-mandate --
Razorpay Plan + Subscription, the account's only approved recurring
primitive. The AFA-free ceiling still decides whether each debit needs
authentication, so the substitution preserves the property that mattered
about the original: an amount over Rs 15,000 needs AFA either way.

**One thing this cannot do: pin the authentication method.** Live-probed
2026-09-01 -- Razorpay rejects `auth_type` on subscription create for this
account outright ("auth_type is/are not required and should not be sent"),
for every value including `netbanking`, `debitcard`, `aadhaar` and `nach`.
The debtor chooses their method on Razorpay's hosted authorization page.
So "netbanking e-mandate" describes the instrument class this account can
issue, not a channel this code can force -- and saying otherwise would be
claiming a control that was tested for and does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.mandate.instrument import AFA_FREE_CEILING_PAISE, InstrumentSpec, InstrumentType

UNAVAILABLE_ON_THIS_ACCOUNT = {
    InstrumentType.UPI_AUTOPAY_ONE_TIME,
    InstrumentType.UPI_BLOCK_RESERVE_PAY,
}
"""UPI Autopay is not approved on this Razorpay account. This is a fact
about the account, not about the instruments -- both are correct §12.2
answers, and on an approved account this set would be empty."""

UNAVAILABLE_REASON = (
    "UPI Autopay is not approved on this Razorpay account "
    "(docs/RAIL_CAPABILITIES.md: needs explicit enablement)"
)


@dataclass(frozen=True, slots=True)
class DeployableInstrument:
    """What will actually be created, alongside what was recommended."""

    recommended: InstrumentType
    """`select_instrument()`'s answer, preserved verbatim. Reported even --
    especially -- when it differs from what was issued."""
    deployable: InstrumentType
    amount_paise: int
    requires_afa: bool
    substituted: bool
    reason: str
    """Why the substitution happened, or the original rationale when none
    was needed."""

    @property
    def is_emandate(self) -> bool:
        return self.deployable in (
            InstrumentType.RECURRING_EMANDATE,
            InstrumentType.RECURRING_EMANDATE_AFA_PER_DEBIT,
        )


def deployable_instrument(spec: InstrumentSpec) -> DeployableInstrument:
    """The instrument this account can actually issue for `spec`.

    A recommendation the rail supports passes through untouched with
    `substituted=False` -- the common case, and it must stay
    indistinguishable from having no capability layer at all.
    """
    if spec.instrument not in UNAVAILABLE_ON_THIS_ACCOUNT:
        return DeployableInstrument(
            recommended=spec.instrument, deployable=spec.instrument,
            amount_paise=spec.amount_paise, requires_afa=spec.requires_afa,
            substituted=False, reason=spec.rationale,
        )

    # The AFA-free ceiling is the property worth preserving across the
    # substitution: an amount over Rs 15,000 needs authentication whichever
    # instrument carries it, and re-deriving it here from the same constant
    # keeps that true rather than inheriting it by luck.
    over_ceiling = spec.amount_paise > AFA_FREE_CEILING_PAISE
    substitute = (
        InstrumentType.RECURRING_EMANDATE_AFA_PER_DEBIT if over_ceiling
        else InstrumentType.RECURRING_EMANDATE
    )
    return DeployableInstrument(
        recommended=spec.instrument, deployable=substitute,
        amount_paise=spec.amount_paise, requires_afa=over_ceiling,
        substituted=True,
        reason=(
            f"{spec.instrument.value} is the §12.2 recommendation, but {UNAVAILABLE_REASON}. "
            f"Issuing a netbanking/eNACH e-mandate instead"
            + (" -- over the Rs 15,000 AFA-free ceiling, so each debit needs authentication."
               if over_ceiling else
               " -- at or under the Rs 15,000 AFA-free ceiling, so one authorization covers it.")
        ),
    )


def describe_deployment(deployed: DeployableInstrument) -> str:
    """One line for a human or for the composer's context block."""
    if not deployed.substituted:
        return f"Instrument: {deployed.deployable.value}"
    return (
        f"Instrument: {deployed.deployable.value} "
        f"(recommended {deployed.recommended.value}; {UNAVAILABLE_REASON})"
    )
