"""SimulatedRail: identical interface to RazorpayRail, HMAC-signed webhooks. DEVDOC_v6 §5.3.

Two hard constraints from the spec, both enforced here rather than left to
good intentions:

1. §5.5 — the simulator may only ever emit a failure code that exists in
   data/failure_taxonomy.yaml. `present_debit`'s failure policy is checked
   against that surface at call time.
2. §12.5 — "SimulatedRail enforces [the NOTIFIED_24H gate] too — the simulator
   must be at least as strict as the regulation, never more permissive."
   `present_debit` refuses a debit that wasn't notified >=24h ahead, using
   the injectable `clock`, independent of whatever BOUNDS decides.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal

from agent.diagnose.taxonomy import FailureTaxonomy, default_taxonomy
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
    RefundResult,
)
from agent.rails.webhook_signing import sign

Clock = Callable[[], datetime]


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(7)}"


FailurePolicy = Callable[[str, int], tuple[str, Literal["cards", "upi"]] | None]
"""Given (mandate_id, amount_paise), return (failure_code, rail) to fail the debit,
or None to succeed. Default policy always succeeds — see `always_succeeds`."""


def always_succeeds(mandate_id: str, amount_paise: int) -> tuple[str, str] | None:
    return None


@dataclass(frozen=True, slots=True)
class SignedWebhook:
    event_id: str
    event_type: str
    body: bytes
    signature: str


class SimulatedRailError(Exception):
    """Raised for a simulated call that a real Razorpay call would also refuse —
    e.g. presenting a debit that was never notified 24h ahead (§12.5)."""


@dataclass
class _MandateRecord:
    mandate: Mandate
    last_notified_at: datetime | None = None
    consecutive_nsf: int = 0


class SimulatedRail:
    """In-memory Rail implementation. One process, not durable — tests and the
    eval harness own the process lifetime, same as any other test double."""

    rail_tag: Literal["simulated"] = "simulated"

    def __init__(
        self,
        webhook_secret: str,
        *,
        taxonomy: FailureTaxonomy | None = None,
        failure_policy: FailurePolicy = always_succeeds,
        clock: Clock = _default_clock,
        on_webhook: Callable[[SignedWebhook], None] | None = None,
    ):
        self._secret = webhook_secret
        self._taxonomy = taxonomy or default_taxonomy()
        self._failure_policy = failure_policy
        self._clock = clock
        self._on_webhook = on_webhook

        self._orders: dict[str, Order] = {}
        self._links: dict[str, PaymentLink] = {}
        self._invoices: dict[str, Invoice] = {}
        self._mandates: dict[str, _MandateRecord] = {}
        self._payments: dict[str, dict] = {}
        self._refunds: dict[str, RefundResult] = {}
        self.emitted_webhooks: list[SignedWebhook] = []

    # ---- Rail protocol ----

    def create_order(self, spec: OrderSpec) -> Order:
        order = Order(id=_new_id("order"), amount_paise=spec.amount_paise, currency=spec.currency,
                       status="created", receipt=spec.receipt)
        self._orders[order.id] = order
        return order

    def create_payment_link(self, spec: LinkSpec) -> PaymentLink:
        link = PaymentLink(
            id=_new_id("plink"),
            short_url=f"https://rzp.io/i/{secrets.token_hex(4)}",
            amount_paise=spec.amount_paise,
            status="created",
        )
        self._links[link.id] = link
        return link

    def create_invoice(self, spec: InvoiceSpec) -> Invoice:
        invoice = Invoice(
            id=_new_id("inv"),
            amount_paise=spec.amount_paise,
            status="issued",
            short_url=f"https://rzp.io/i/{secrets.token_hex(4)}",
        )
        self._invoices[invoice.id] = invoice
        return invoice

    def create_mandate(self, spec: MandateSpec) -> Mandate:
        mandate = Mandate(
            id=_new_id("sub"),
            rail="simulated",
            max_amount_paise=spec.max_amount_paise,
            start_at=spec.start_at,
            end_at=spec.end_at,
            status="pending_afa",
            afa_required=spec.afa_required,
            debit_schedule=list(spec.debit_schedule),
            short_url=f"https://rzp.io/i/{secrets.token_hex(4)}",
        )
        self._mandates[mandate.id] = _MandateRecord(mandate=mandate)
        return mandate

    def modify_mandate(self, mandate_id: str, delta: MandateDelta) -> Mandate:
        record = self._require_mandate(mandate_id)
        updated = record.mandate.model_copy(
            update={
                k: v
                for k, v in {
                    "max_amount_paise": delta.max_amount_paise,
                    "end_at": delta.end_at,
                }.items()
                if v is not None
            }
        )
        record.mandate = updated
        return updated

    def revoke_mandate(self, mandate_id: str) -> Mandate:
        record = self._require_mandate(mandate_id)
        record.mandate = record.mandate.model_copy(update={"status": "revoked"})
        self._emit(
            "mandate.revoked",
            {"mandate": {"entity": {"id": mandate_id, "status": "revoked"}}},
        )
        return record.mandate

    def create_refund(self, payment_id: str, reason: str) -> RefundResult:
        payment = self._payments.get(payment_id)
        if payment is None:
            raise SimulatedRailError(f"cannot refund unknown payment_id={payment_id!r}")
        if payment["status"] != "captured":
            raise SimulatedRailError(
                f"cannot refund payment_id={payment_id!r} in status={payment['status']!r} — only captured payments are refundable"
            )
        refund = RefundResult(
            id=_new_id("rfnd"), payment_id=payment_id, amount_paise=payment["amount_paise"], status="processed"
        )
        self._refunds[refund.id] = refund
        self._emit(
            "refund.processed",
            {"refund": {"entity": {"id": refund.id, "payment_id": payment_id,
                                    "amount": refund.amount_paise, "status": "processed",
                                    "notes": {"reason": reason}}}},
        )
        return refund

    def fetch(self, kind: str, id: str) -> dict:
        store = {
            "orders": self._orders,
            "payment_links": self._links,
            "invoices": self._invoices,
            "payments": self._payments,
            "refunds": self._refunds,
        }.get(kind)
        if store is None:
            raise SimulatedRailError(f"unknown fetch kind={kind!r}")
        if kind == "payments":
            obj = store.get(id)
            if obj is None:
                raise SimulatedRailError(f"no {kind[:-1]} with id={id!r}")
            return dict(obj)
        record = store.get(id)
        if record is None:
            raise SimulatedRailError(f"no {kind[:-1]} with id={id!r}")
        return record.model_dump()

    # ---- Mandate notification + debit presentment (§12.4, §12.5) ----

    def notify_predebit(self, mandate_id: str, amount_paise: int, debit_datetime: str, reason: str) -> None:
        """Send the mandatory pre-debit notice. Sets last_notified_at from the injected
        clock — present_debit() checks this, not caller-supplied timing, so a caller
        can't claim a notification happened earlier than it did."""
        record = self._require_mandate(mandate_id)
        record.last_notified_at = self._clock()
        record.mandate = record.mandate.model_copy(
            update={"status": "notified_24h", "last_notification_at": record.last_notified_at.isoformat()}
        )
        self._emit(
            "mandate.predebit_notified",
            {
                "mandate": {
                    "entity": {
                        "id": mandate_id,
                        "amount": amount_paise,
                        "debit_datetime": debit_datetime,
                        "reason": reason,
                    }
                }
            },
        )

    def present_debit(self, mandate_id: str, amount_paise: int) -> DebitResult:
        record = self._require_mandate(mandate_id)

        if record.mandate.status == "revoked":
            raise SimulatedRailError(f"mandate {mandate_id!r} is revoked — cannot present a debit")

        if record.last_notified_at is None:
            raise SimulatedRailError(
                f"mandate {mandate_id!r} was never sent a pre-debit notification — "
                "refusing to present (§12.5's NOTIFIED_24H gate; SimulatedRail must be "
                "at least as strict as the regulation)"
            )
        elapsed = self._clock() - record.last_notified_at
        if elapsed < timedelta(hours=24):
            raise SimulatedRailError(
                f"mandate {mandate_id!r} was notified only {elapsed} ago (<24h) — refusing to "
                "present the debit (RBI_EMANDATE_PREDEBIT_24H, enforced here independent of BOUNDS)"
            )

        failure = self._failure_policy(mandate_id, amount_paise)
        if failure is not None:
            code, rail = failure
            if code not in self._taxonomy.permitted_codes(rail):
                raise SimulatedRailError(
                    f"failure_policy returned code={code!r} rail={rail!r}, which is outside the "
                    "taxonomy's permitted failure surface (§5.4/§5.5) — the simulator refuses to "
                    "emit behaviour that isn't sourced"
                )
            record.consecutive_nsf += 1
            result = DebitResult(
                payment_id=_new_id("pay"), mandate_id=mandate_id, amount_paise=amount_paise,
                status="failed", failure_code=code, failure_rail=rail,
            )
            self._payments[result.payment_id] = {
                "id": result.payment_id, "amount_paise": amount_paise, "status": "failed",
                "error_code": code, "error_source": rail,
            }
            self._emit(
                "payment.failed",
                {"payment": {"entity": {"id": result.payment_id, "amount": amount_paise,
                                         "status": "failed", "error_code": code}}},
            )
            return result

        record.consecutive_nsf = 0
        result = DebitResult(
            payment_id=_new_id("pay"), mandate_id=mandate_id, amount_paise=amount_paise, status="captured",
        )
        self._payments[result.payment_id] = {
            "id": result.payment_id, "amount_paise": amount_paise, "status": "captured",
        }
        self._emit(
            "payment.captured",
            {"payment": {"entity": {"id": result.payment_id, "amount": amount_paise, "status": "captured"}}},
        )
        return result

    # ---- Simulation-only helpers: NOT part of the Rail protocol ----
    # A test or the eval harness drives "the customer acted" through these —
    # RazorpayRail has no equivalent, because in reality the customer's own
    # action (paying a link, completing AFA) is what produces these events,
    # not a call our system makes. Conformance tests must not expect these on
    # RazorpayRail.

    def simulate_afa_completion(self, mandate_id: str) -> Mandate:
        record = self._require_mandate(mandate_id)
        record.mandate = record.mandate.model_copy(update={"status": "active"})
        self._emit("mandate.activated", {"mandate": {"entity": {"id": mandate_id, "status": "active"}}})
        return record.mandate

    def simulate_link_paid(self, link_id: str) -> PaymentLink:
        link = self._links.get(link_id)
        if link is None:
            raise SimulatedRailError(f"no payment_link with id={link_id!r}")
        updated = link.model_copy(update={"status": "paid"})
        self._links[link_id] = updated
        payment_id = _new_id("pay")
        self._payments[payment_id] = {"id": payment_id, "amount_paise": link.amount_paise, "status": "captured"}
        self._emit(
            "payment_link.paid",
            {"payment_link": {"entity": {"id": link_id, "status": "paid"}},
             "payment": {"entity": {"id": payment_id, "amount": link.amount_paise, "status": "captured"}}},
        )
        return updated

    def simulate_invoice_paid(self, invoice_id: str) -> Invoice:
        invoice = self._invoices.get(invoice_id)
        if invoice is None:
            raise SimulatedRailError(f"no invoice with id={invoice_id!r}")
        updated = invoice.model_copy(update={"status": "paid"})
        self._invoices[invoice_id] = updated
        payment_id = _new_id("pay")
        self._payments[payment_id] = {"id": payment_id, "amount_paise": invoice.amount_paise, "status": "captured"}
        self._emit(
            "invoice.paid",
            {"invoice": {"entity": {"id": invoice_id, "status": "paid"}},
             "payment": {"entity": {"id": payment_id, "amount": invoice.amount_paise, "status": "captured"}}},
        )
        return updated

    # ---- internals ----

    def _require_mandate(self, mandate_id: str) -> _MandateRecord:
        record = self._mandates.get(mandate_id)
        if record is None:
            raise SimulatedRailError(f"no mandate with id={mandate_id!r}")
        return record

    def _emit(self, event_type: str, payload: dict) -> SignedWebhook:
        import json

        event_id = _new_id("evt")
        envelope = {
            "event": event_type,
            "event_id": event_id,
            "created_at": self._clock().isoformat(),
            "payload": payload,
        }
        body = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signed = SignedWebhook(event_id=event_id, event_type=event_type, body=body, signature=sign(body, self._secret))
        self.emitted_webhooks.append(signed)
        if self._on_webhook is not None:
            self._on_webhook(signed)
        return signed
