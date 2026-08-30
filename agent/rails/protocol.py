"""The Rail Protocol — one interface, two implementations. DEVDOC_v6 §5.3.

Every concrete Rail exposes its own `rail_tag` so the ACT stage can tag every
call in the ledger without the caller having to know which implementation it
holds (Law 6).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent.rails.types import (
    DebitResult,
    Invoice,
    InvoiceSpec,
    LinkSpec,
    Mandate,
    MandateDelta,
    MandateSpec,
    Order,
    OrderSpec,
    PaymentLink,
    RailTag,
    RefundResult,
)


@runtime_checkable
class Rail(Protocol):
    rail_tag: RailTag

    def create_order(self, spec: OrderSpec) -> Order: ...
    def create_payment_link(self, spec: LinkSpec) -> PaymentLink: ...
    def create_invoice(self, spec: InvoiceSpec) -> Invoice: ...
    def create_mandate(self, spec: MandateSpec) -> Mandate: ...
    def modify_mandate(self, mandate_id: str, delta: MandateDelta) -> Mandate: ...
    def present_debit(self, mandate_id: str, amount_paise: int) -> DebitResult: ...
    def revoke_mandate(self, mandate_id: str) -> Mandate: ...
    def create_refund(self, payment_id: str, reason: str) -> RefundResult: ...
    def fetch(self, kind: str, id: str) -> dict: ...
