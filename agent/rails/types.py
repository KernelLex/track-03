"""Shared request/response shapes for the Rail protocol. DEVDOC_v6 §5.3.

Deliberately a subset of the real Razorpay API's fields — enough for object
shape and state-transition conformance (§5.4's in-scope claims), not a full
mirror of the API. Amounts are always paise-as-int (§9.1); never a float field.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RailTag = Literal["razorpay", "simulated"]


class RailUnavailable(Exception):
    """Raised by RazorpayRail for any capability beyond what the day-zero probe found
    reachable on this account (§5.1, §6) — mandate rails on an unactivated account,
    most likely. Never silently falls back to simulated behaviour; the caller decides."""

    def __init__(self, capability: str, detail: str = ""):
        self.capability = capability
        self.detail = detail
        super().__init__(f"rail capability unavailable: {capability}" + (f" — {detail}" if detail else ""))


class OrderSpec(BaseModel):
    amount_paise: int = Field(gt=0)
    currency: Literal["INR"] = "INR"
    receipt: str | None = None
    notes: dict[str, str] = Field(default_factory=dict)


class Order(BaseModel):
    id: str
    amount_paise: int
    currency: Literal["INR"] = "INR"
    status: Literal["created", "attempted", "paid"]
    receipt: str | None = None


class LinkSpec(BaseModel):
    amount_paise: int = Field(gt=0)
    description: str
    customer_contact: str | None = None
    customer_email: str | None = None
    expire_by: str | None = None
    notes: dict[str, str] = Field(default_factory=dict)


class PaymentLink(BaseModel):
    id: str
    short_url: str
    amount_paise: int
    status: Literal["created", "paid", "cancelled", "expired", "partially_paid"]


class InvoiceSpec(BaseModel):
    amount_paise: int = Field(gt=0)
    description: str
    customer_contact: str | None = None
    customer_email: str | None = None
    customer_name: str | None = None
    notes: dict[str, str] = Field(default_factory=dict)


class Invoice(BaseModel):
    id: str
    amount_paise: int
    status: Literal["draft", "issued", "partially_paid", "paid", "cancelled", "expired"]
    short_url: str | None = None


class MandateSpec(BaseModel):
    max_amount_paise: int = Field(gt=0)
    start_at: str
    end_at: str
    debit_schedule: list[str] = Field(default_factory=list)
    afa_required: bool = True
    customer_contact: str | None = None


class MandateDelta(BaseModel):
    max_amount_paise: int | None = None
    end_at: str | None = None


class Mandate(BaseModel):
    id: str
    rail: RailTag
    max_amount_paise: int
    start_at: str
    end_at: str
    status: Literal[
        "created", "pending_afa", "active", "health_defect", "repairing",
        "debit_scheduled", "notified_24h", "revoked", "expired",
    ]
    afa_required: bool
    debit_schedule: list[str] = Field(default_factory=list)
    last_notification_at: str | None = None


class DebitResult(BaseModel):
    payment_id: str
    mandate_id: str
    amount_paise: int
    status: Literal["captured", "failed", "pending"]
    failure_code: str | None = None
    failure_rail: Literal["cards", "upi"] | None = None


class RefundResult(BaseModel):
    id: str
    payment_id: str
    amount_paise: int
    status: Literal["pending", "processed", "failed"]
