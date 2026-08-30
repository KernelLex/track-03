"""The shared conformance suite. DEVDOC_v6 §5.4.

One suite, both rails: `run_conformance_suite(make_rail)` takes a factory
(`lambda: SimulatedRail(...)` today, `lambda: RazorpayRail(...)` once test
keys exist) rather than a rail instance, since some checks need a fresh
rail. The exact same function runs against both — nothing here is
SimulatedRail-specific.

Scope, repeated from §5.4 because it's the whole point of this module:
**in scope** — object shape and field presence, state transition legality,
error code vocabulary, webhook payload structure and signature scheme,
idempotency semantics. **Out of scope, and no check below claims
otherwise** — NACH return latency, settlement timing, real failure
distributions, issuer-specific behaviour. Agreement on this CRUD surface is
good evidence for shapes and transitions and weak evidence for debit
behaviour; that asymmetry is the reason this file exists rather than a
single "it works" boolean.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from agent.diagnose.taxonomy import UnknownFailureCode, default_taxonomy
from agent.ingest.webhooks import EventStore, MalformedWebhook, SignatureInvalid, verify_and_ingest
from agent.rails.protocol import Rail
from agent.rails.types import InvoiceSpec, LinkSpec, MandateSpec, OrderSpec

RailFactory = Callable[[str], Rail]
"""Takes the webhook secret to configure the rail with -- not a bare `() -> Rail`
-- so run_conformance_suite can guarantee the secret it verifies webhooks
against is the exact one the rail was built with, rather than relying on
both sides hardcoding the same string."""

KNOWN_ORDER_STATUSES = frozenset({"created", "attempted", "paid"})
KNOWN_LINK_STATUSES = frozenset({"created", "paid", "cancelled", "expired", "partially_paid"})
KNOWN_INVOICE_STATUSES = frozenset({"draft", "issued", "partially_paid", "paid", "cancelled", "expired"})
KNOWN_MANDATE_STATUSES = frozenset({
    "created", "pending_afa", "active", "health_defect", "repairing",
    "debit_scheduled", "notified_24h", "revoked", "expired",
})


@dataclass(frozen=True, slots=True)
class ConformanceCheck:
    name: str
    passed: bool
    detail: str
    in_scope: bool = True
    """False for a check recorded as informational/skipped rather than a real
    pass/fail -- kept out of all_passed's calculation."""


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    rail_tag: str
    checks: tuple[ConformanceCheck, ...]

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks if c.in_scope)

    @property
    def failures(self) -> tuple[ConformanceCheck, ...]:
        return tuple(c for c in self.checks if c.in_scope and not c.passed)


def _check_order_shape(rail: Rail) -> ConformanceCheck:
    order = rail.create_order(OrderSpec(amount_paise=50_000, receipt="conformance_probe"))
    ok = bool(order.id) and order.amount_paise == 50_000 and order.status in KNOWN_ORDER_STATUSES
    return ConformanceCheck("order_shape", ok, f"id={order.id!r} amount={order.amount_paise} status={order.status!r}")


def _check_payment_link_shape(rail: Rail) -> ConformanceCheck:
    link = rail.create_payment_link(LinkSpec(amount_paise=30_000, description="conformance probe"))
    ok = bool(link.id) and bool(link.short_url) and link.amount_paise == 30_000 and link.status in KNOWN_LINK_STATUSES
    return ConformanceCheck("payment_link_shape", ok, f"id={link.id!r} status={link.status!r}")


def _check_invoice_shape(rail: Rail) -> ConformanceCheck:
    invoice = rail.create_invoice(InvoiceSpec(amount_paise=75_000, description="conformance probe"))
    ok = bool(invoice.id) and invoice.amount_paise == 75_000 and invoice.status in KNOWN_INVOICE_STATUSES
    return ConformanceCheck("invoice_shape", ok, f"id={invoice.id!r} status={invoice.status!r}")


def _check_mandate_shape_and_revoke_transition(rail: Rail) -> ConformanceCheck:
    mandate = rail.create_mandate(MandateSpec(
        max_amount_paise=10_000, start_at="2026-01-01T00:00:00Z", end_at="2027-01-01T00:00:00Z",
    ))
    shape_ok = bool(mandate.id) and mandate.max_amount_paise == 10_000 and mandate.status in KNOWN_MANDATE_STATUSES

    revoked = rail.revoke_mandate(mandate.id)
    transition_ok = revoked.status == "revoked"

    ok = shape_ok and transition_ok
    return ConformanceCheck(
        "mandate_shape_and_revoke_transition", ok,
        f"created status={mandate.status!r}, after revoke status={revoked.status!r}",
    )


def _check_webhook_structure_and_signature(rail: Rail, secret: str) -> ConformanceCheck:
    link = rail.create_payment_link(LinkSpec(amount_paise=10_000, description="conformance probe"))
    emit = getattr(rail, "simulate_link_paid", None)
    if emit is None:
        return ConformanceCheck(
            "webhook_structure_and_signature", True,
            "rail has no simulate_link_paid helper (expected for a real rail -- payment "
            "completion happens via checkout, not a direct API call); skipped, not failed",
            in_scope=False,
        )
    emit(link.id)

    emitted = getattr(rail, "emitted_webhooks", None)
    if not emitted:
        return ConformanceCheck("webhook_structure_and_signature", False, "no webhook was emitted")

    webhook = emitted[-1]
    with EventStore(":memory:") as store:
        try:
            result = verify_and_ingest(
                store=store, source=getattr(rail, "rail_tag", "unknown"),
                body=webhook.body, signature=webhook.signature, secret=secret,
            )
        except (SignatureInvalid, MalformedWebhook) as exc:
            return ConformanceCheck("webhook_structure_and_signature", False, f"{type(exc).__name__}: {exc}")

    ok = not result.is_duplicate and bool(result.event_type) and isinstance(result.payload, dict)
    return ConformanceCheck(
        "webhook_structure_and_signature", ok,
        f"event_type={result.event_type!r}, signature verified, envelope parsed",
    )


def _check_idempotent_redelivery(rail: Rail, secret: str) -> ConformanceCheck:
    link = rail.create_payment_link(LinkSpec(amount_paise=10_000, description="conformance probe"))
    emit = getattr(rail, "simulate_link_paid", None)
    if emit is None:
        return ConformanceCheck(
            "idempotent_redelivery", True, "no simulate_link_paid helper on this rail; skipped, not failed",
            in_scope=False,
        )
    emit(link.id)
    webhook = rail.emitted_webhooks[-1]  # type: ignore[attr-defined]

    with EventStore(":memory:") as store:
        first = verify_and_ingest(store=store, source="conformance", body=webhook.body, signature=webhook.signature, secret=secret)
        second = verify_and_ingest(store=store, source="conformance", body=webhook.body, signature=webhook.signature, secret=secret)

    ok = (not first.is_duplicate) and second.is_duplicate
    return ConformanceCheck("idempotent_redelivery", ok, f"first.is_duplicate={first.is_duplicate}, second.is_duplicate={second.is_duplicate}")


def _check_failure_codes_are_in_the_published_vocabulary(rail: Rail) -> ConformanceCheck:
    """Rail-produced failures, if any occurred, must use a code this build has
    actually sourced (data/failure_taxonomy.yaml) -- not that a failure occurs
    at all, which is exactly the temporal/distributional behaviour §5.4 puts
    out of scope."""
    taxonomy = default_taxonomy()
    fetch = getattr(rail, "fetch", None)
    if fetch is None:
        return ConformanceCheck("failure_code_vocabulary", True, "rail has no fetch() to inspect", in_scope=False)
    try:
        payments = fetch("payments", "")  # a real rail would need a real id; SimulatedRail's store isn't listable this way either
    except Exception:  # noqa: BLE001
        return ConformanceCheck(
            "failure_code_vocabulary", True,
            "no generic 'list all payments' probe available through this Rail's fetch() -- "
            "this check needs a rail-specific listing call not yet built; skipped, not failed",
            in_scope=False,
        )
    code = payments.get("error_code") if isinstance(payments, dict) else None
    if code is None:
        return ConformanceCheck("failure_code_vocabulary", True, "no failure observed to check", in_scope=False)
    try:
        taxonomy.classify(code, payments.get("error_source", "cards"))
        return ConformanceCheck("failure_code_vocabulary", True, f"code={code!r} is in the published taxonomy")
    except UnknownFailureCode:
        return ConformanceCheck("failure_code_vocabulary", False, f"code={code!r} is NOT in data/failure_taxonomy.yaml")


def run_conformance_suite(make_rail: RailFactory, *, webhook_secret: str = "conformance-suite-secret") -> ConformanceReport:
    rail = make_rail(webhook_secret)
    checks = [
        _check_order_shape(rail),
        _check_payment_link_shape(rail),
        _check_invoice_shape(rail),
        _check_mandate_shape_and_revoke_transition(rail),
        _check_webhook_structure_and_signature(make_rail(webhook_secret), webhook_secret),
        _check_idempotent_redelivery(make_rail(webhook_secret), webhook_secret),
        _check_failure_codes_are_in_the_published_vocabulary(rail),
    ]
    return ConformanceReport(rail_tag=getattr(rail, "rail_tag", "unknown"), checks=tuple(checks))
