"""select_instrument() — the core mandate-layer function. DEVDOC_v6 §12.2.

Pure and rail-independent: given what the debtor stated (and what's true
about the invoice), decide which payment instrument to deploy. Correctness
at the Rs 15,000 AFA-free boundary is Tier-1 measured (§17.7) — every row
of §12.2's table is a test case here, parametrized directly on that table.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

AFA_FREE_CEILING_PAISE = 1_500_000
"""Rs 15,000, in paise — same constant the RBI_EMANDATE_AFA_CEILING bounds rule
enforces independently (agent/bounds/rules.yaml). Duplicated deliberately: one
is a declarative YAML gate, the other is this pure decision function: two
places checking the same regulatory fact from different angles, not one
place trusting the other."""


class InstrumentType(str, Enum):
    UPI_AUTOPAY_ONE_TIME = "upi_autopay_one_time"
    UPI_BLOCK_RESERVE_PAY = "upi_block_reserve_pay"
    RECURRING_EMANDATE = "recurring_emandate"
    RECURRING_EMANDATE_AFA_PER_DEBIT = "recurring_emandate_afa_per_debit"
    PAYMENT_LINK_UNDISPUTED_PORTION = "payment_link_undisputed_portion"
    PAYMENT_LINK_PLUS_REMINDER = "payment_link_plus_reminder"


@dataclass(frozen=True, slots=True)
class Promise:
    """What the debtor stated — MODEL-provenance candidate input only (Law 5);
    never itself a legal fact. §11.2's extraction schema is the usual source."""

    total_amount_paise: int
    installments: int = 1
    installment_amount_paise: int | None = None
    """Required when installments > 1 — the amount of *each* installment, not
    derived by dividing total_amount_paise, since a real payment plan need not
    divide evenly (§12.2 talks about "each" installment's size directly)."""
    declined: bool = False


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    instrument: InstrumentType
    amount_paise: int
    """The amount *this instrument* targets — may be less than the promise's
    total when part of it is disputed (never a mandate on a contested amount)."""
    requires_afa: bool
    rationale: str


class InvalidPromise(Exception):
    """Raised for a Promise that doesn't have enough information to instrument —
    e.g. installments > 1 with no per-installment amount. A missing value here
    must not silently become a guess."""


def select_instrument(promise: Promise, *, disputed_paise: int = 0) -> InstrumentSpec:
    if disputed_paise < 0:
        raise ValueError("disputed_paise cannot be negative")
    if disputed_paise > promise.total_amount_paise:
        raise ValueError("disputed_paise cannot exceed the promise's total")

    # Dispute takes priority over everything else: §12.2's own principle
    # ("never a mandate on a contested amount") and NO_MANDATE_ON_DISPUTE
    # (agent/bounds/rules.yaml) both express this — this function honours it
    # structurally, by routing away from every mandate-shaped instrument
    # before disputed_paise is even considered against the AFA ceiling.
    if disputed_paise > 0:
        undisputed = promise.total_amount_paise - disputed_paise
        return InstrumentSpec(
            instrument=InstrumentType.PAYMENT_LINK_UNDISPUTED_PORTION,
            amount_paise=undisputed,
            requires_afa=False,
            rationale="amount partially disputed — never a mandate on a contested amount; "
                      "a payment link for the undisputed remainder only",
        )

    if promise.declined:
        return InstrumentSpec(
            instrument=InstrumentType.PAYMENT_LINK_PLUS_REMINDER,
            amount_paise=promise.total_amount_paise,
            requires_afa=False,
            rationale="debtor declined an instrument — fall back to a payment link and a "
                      "reminder; the refusal itself is logged by the caller and feeds EV",
        )

    if promise.installments <= 1:
        if promise.total_amount_paise <= AFA_FREE_CEILING_PAISE:
            return InstrumentSpec(
                instrument=InstrumentType.UPI_AUTOPAY_ONE_TIME,
                amount_paise=promise.total_amount_paise,
                requires_afa=False,
                rationale="single payment at or under the Rs 15,000 AFA-free ceiling",
            )
        return InstrumentSpec(
            instrument=InstrumentType.UPI_BLOCK_RESERVE_PAY,
            amount_paise=promise.total_amount_paise,
            requires_afa=True,
            rationale="single payment over Rs 15,000 — funds blocked with AFA at "
                      "commitment; the later debit isn't a new decision",
        )

    if promise.installment_amount_paise is None:
        raise InvalidPromise(
            "installments > 1 requires installment_amount_paise — refusing to guess "
            "an even split of total_amount_paise"
        )

    if promise.installment_amount_paise <= AFA_FREE_CEILING_PAISE:
        return InstrumentSpec(
            instrument=InstrumentType.RECURRING_EMANDATE,
            amount_paise=promise.installment_amount_paise,
            requires_afa=False,
            rationale=f"{promise.installments} installments, each at or under the AFA-free "
                      "ceiling — one authorization replaces 6-9 chase touches",
        )
    return InstrumentSpec(
        instrument=InstrumentType.RECURRING_EMANDATE_AFA_PER_DEBIT,
        amount_paise=promise.installment_amount_paise,
        requires_afa=True,
        rationale=f"{promise.installments} installments, each over the AFA-free ceiling — "
                  "the AFA link ships inside the mandatory pre-debit notification",
    )
