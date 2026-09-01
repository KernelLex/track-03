"""Path B extraction schema — the contract, not the model call. DEVDOC_v6 §11.2.

This is the JSON schema every model extraction must validate against before
anything downstream reads it. It exists and is fully testable without a live
LLM: the schema, the family/class consistency rule, and everything built on
top of an `ExtractionResult` (§24.1's injection-resistance tests included)
work the same whether an `ExtractionResult` came from a real model call or
was constructed directly in a test to stand in for "whatever a compromised
model might have produced." Building `ExtractionResult` by hand is not
allowed anywhere outside a test — see the field's own docstring.
"""

from __future__ import annotations

from datetime import date, timedelta
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_PROMISE_DATE_HORIZON_DAYS = 730
"""~2 years. A payment "promise" further out than this (or in the past) isn't
a real payment plan -- DEVDOC_v6 §24.1 names "a date decades out" as exactly
the kind of out-of-range value Pydantic validation should catch, which an
unconstrained str field does not do by itself."""

GSTIN_PATTERN = r"^[0-9]{2}[A-Z0-9]{10}[0-9][A-Z][0-9A-Z]$"
"""Structural shape only (15 chars: state code, PAN, entity code, checksum
slot) -- not a real checksum validation, which is out of scope here."""


class Family(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


class DiagnosisClass(str, Enum):
    # Family A -- instrument failure
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    INSTRUMENT_EXPIRED = "INSTRUMENT_EXPIRED"
    MANDATE_INVALID = "MANDATE_INVALID"
    BANK_DOWNTIME = "BANK_DOWNTIME"
    AUTH_FAILURE = "AUTH_FAILURE"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    CUSTOMER_ABANDONED = "CUSTOMER_ABANDONED"
    HEADROOM_BREACH = "HEADROOM_BREACH"
    EXPIRY_BEFORE_DEBIT = "EXPIRY_BEFORE_DEBIT"
    AFA_THRESHOLD_BREACH = "AFA_THRESHOLD_BREACH"
    REPEAT_NSF = "REPEAT_NSF"
    SILENT_REVOCATION = "SILENT_REVOCATION"
    RAIL_DEGRADED = "RAIL_DEGRADED"
    # Family B -- administrative blocker
    INVOICE_NOT_RECEIVED = "INVOICE_NOT_RECEIVED"
    PO_MISMATCH = "PO_MISMATCH"
    GST_DEFECT = "GST_DEFECT"
    ALREADY_PAID_UNRECONCILED = "ALREADY_PAID_UNRECONCILED"
    APPROVAL_BOTTLENECK = "APPROVAL_BOTTLENECK"
    DOCUMENT_MISSING = "DOCUMENT_MISSING"
    BANK_DETAIL_MISMATCH = "BANK_DETAIL_MISMATCH"
    # Family C -- liquidity / willingness
    CASHFLOW_SHORTFALL = "CASHFLOW_SHORTFALL"
    PROMISE_STATED = "PROMISE_STATED"
    SILENT = "SILENT"
    STALLING = "STALLING"
    REFUSAL = "REFUSAL"
    # Family D -- dispute
    QUANTITY_QUALITY = "QUANTITY_QUALITY"
    AMOUNT = "AMOUNT"
    CONTRACT = "CONTRACT"
    NOT_OUR_DEBT = "NOT_OUR_DEBT"


FAMILY_CLASSES: dict[Family, frozenset[DiagnosisClass]] = {
    Family.A: frozenset({
        DiagnosisClass.INSUFFICIENT_FUNDS, DiagnosisClass.INSTRUMENT_EXPIRED,
        DiagnosisClass.MANDATE_INVALID, DiagnosisClass.BANK_DOWNTIME,
        DiagnosisClass.AUTH_FAILURE, DiagnosisClass.LIMIT_EXCEEDED,
        DiagnosisClass.CUSTOMER_ABANDONED, DiagnosisClass.HEADROOM_BREACH,
        DiagnosisClass.EXPIRY_BEFORE_DEBIT, DiagnosisClass.AFA_THRESHOLD_BREACH,
        DiagnosisClass.REPEAT_NSF, DiagnosisClass.SILENT_REVOCATION,
        DiagnosisClass.RAIL_DEGRADED,
    }),
    Family.B: frozenset({
        DiagnosisClass.INVOICE_NOT_RECEIVED, DiagnosisClass.PO_MISMATCH,
        DiagnosisClass.GST_DEFECT, DiagnosisClass.ALREADY_PAID_UNRECONCILED,
        DiagnosisClass.APPROVAL_BOTTLENECK, DiagnosisClass.DOCUMENT_MISSING,
        DiagnosisClass.BANK_DETAIL_MISMATCH,
    }),
    Family.C: frozenset({
        DiagnosisClass.CASHFLOW_SHORTFALL, DiagnosisClass.PROMISE_STATED,
        DiagnosisClass.SILENT, DiagnosisClass.STALLING, DiagnosisClass.REFUSAL,
    }),
    Family.D: frozenset({
        DiagnosisClass.QUANTITY_QUALITY, DiagnosisClass.AMOUNT,
        DiagnosisClass.CONTRACT, DiagnosisClass.NOT_OUR_DEBT,
    }),
}

ACTIONS_UNLOCKED: dict[Family, frozenset[str]] = {
    Family.A: frozenset({"retry_charge", "repair_mandate", "create_payment_link"}),
    Family.B: frozenset({"reissue_artifact", "request_reconciliation"}),
    Family.C: frozenset({"create_mandate", "create_payment_link", "send_reminder"}),
    Family.D: frozenset({"escalate_human"}),
}
"""§11.2's table. Even a fully compromised extraction (Family.D chosen by an
attacker) can only unlock `escalate_human` -- there is no family whose action
set includes anything that closes an account or marks it settled without a
rail-confirmed payment (§24.1)."""


def _validate_promise_date(value: str | None) -> str | None:
    """Shared by `PromiseFields.date` and every `PromiseLeg.date`, so a leg
    inside a schedule cannot smuggle past a horizon check the top-level
    field enforces."""
    if value is None:
        return value
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"promise.date {value!r} is not a valid ISO8601 date") from exc
    today = date.today()
    if parsed < today - timedelta(days=30) or parsed > today + timedelta(days=MAX_PROMISE_DATE_HORIZON_DAYS):
        raise ValueError(
            f"promise.date {value!r} is outside a plausible horizon "
            f"(30 days in the past to {MAX_PROMISE_DATE_HORIZON_DAYS} days out) -- "
            "rejecting rather than passing a decades-out date downstream"
        )
    return value


class PromiseLeg(BaseModel):
    """One instalment the debtor actually named.

    `amount_paise=None` inside a multi-leg schedule means "the rest" -- the
    debtor said "21,000 today and the balance on the 5th" and named a date
    without repeating an amount. That is a real thing people say, and the
    remainder is arithmetic the caller can do (total minus what was named),
    not a number the model should be inventing."""

    amount_paise: int | None = Field(default=None, gt=0)
    date: str | None = None

    @field_validator("date")
    @classmethod
    def _leg_date_is_valid_and_plausible(cls, value: str | None) -> str | None:
        return _validate_promise_date(value)


class PromiseFields(BaseModel):
    amount_paise: int | None = Field(default=None, gt=0)
    """A promise of exactly Rs 0 isn't a payment commitment -- must be positive
    when present (§24.1's schema-poisoning class names amount_paise=0 directly)."""
    date: str | None = None
    installments: int | None = Field(default=None, gt=0)
    schedule: list[PromiseLeg] = Field(default_factory=list)
    """Every instalment the debtor named, in the order they said them.

    Added because a single (amount, date) pair structurally cannot hold
    "21,000 today and the rest on the 5th", and the extractor was collapsing
    such offers into one slot -- differently on different runs. The same
    message produced `{date: 2026-09-01, amount: 2100000}` once and
    `{date: 2026-09-05, amount: None}` the next time, and the second reading
    made the system offer a plan for the full balance on the 5th, which is
    not what was said.

    `amount_paise` and `date` above stay populated for the single-payment
    case and for every existing caller; this is additive."""

    @field_validator("date")
    @classmethod
    def _date_is_valid_and_plausible(cls, value: str | None) -> str | None:
        return _validate_promise_date(value)


class DisputeFields(BaseModel):
    claim: str | None = None
    evidence_ref: str | None = None


class EntityFields(BaseModel):
    utr: str | None = None
    po_number: str | None = None
    gstin: str | None = Field(default=None, pattern=GSTIN_PATTERN)
    contact_person: str | None = None
    stated_pay_date: str | None = None


class ExtractionResult(BaseModel):
    """Every field here is MODEL provenance (§8) by construction -- nothing in
    this module ever labels an ExtractionResult's contents SYSTEM or HUMAN.
    Feeds select_instrument() only as a *candidate* under Law 5; must never
    reach legal_computation() -- see agent.ledger.models.assert_legal_provenance.

    Extra fields in the source JSON are rejected outright (`model_config
    extra="forbid"`), not merely ignored -- a schema-poisoning attempt that
    tries to smuggle in a field like `"state": "RECOVERED"` fails validation
    instead of silently passing through as an ignored key (§24.1 class:
    schema poisoning).
    """

    model_config = {"extra": "forbid", "populate_by_name": True}

    family: Family
    class_: DiagnosisClass = Field(alias="class")
    confidence: float = Field(ge=0.0, le=1.0)
    promise: PromiseFields = Field(default_factory=PromiseFields)
    dispute: DisputeFields = Field(default_factory=DisputeFields)
    entities: EntityFields = Field(default_factory=EntityFields)
    objection_signal: bool = False

    @model_validator(mode="after")
    def _class_matches_family(self) -> "ExtractionResult":
        if self.class_ not in FAMILY_CLASSES[self.family]:
            raise ValueError(
                f"class {self.class_.value!r} is not a valid class for family {self.family.value!r}"
            )
        return self

    def actions_unlocked(self) -> frozenset[str]:
        return ACTIONS_UNLOCKED[self.family]
