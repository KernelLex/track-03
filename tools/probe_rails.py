#!/usr/bin/env python3
"""Day-zero capability probe. DEVDOC_v6 §6. Run once, commit the output.

    RAZORPAY_KEY_ID=rzp_test_xxx RAZORPAY_KEY_SECRET=xxx uv run python tools/probe_rails.py

Writes docs/RAIL_CAPABILITIES.md as a dated table: HTTP status, error code,
description, timestamp, per probe. Re-run before submission — capability can
change if the account gets any enablement between now and then.

NOT YET RUN: this script has never executed against a live account (no test
keys were available while building it). It's written from the razorpay-python
SDK's documented interface, not verified against a live response. Treat the
first real run as the actual day-zero measurement DEVDOC_v6 §6 calls for, and
read its output before trusting anything this docstring or §6 "expects".
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import razorpay
from razorpay.errors import BadRequestError, GatewayError, ServerError

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "docs" / "RAIL_CAPABILITIES.md"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    name: str
    ok: bool
    http_status: int | None
    error_code: str | None
    description: str


def _client() -> razorpay.Client:
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        print("RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be set — get test keys from "
              "the Razorpay dashboard (Settings > API Keys). No KYC needed for test mode (§5.1).",
              file=sys.stderr)
        sys.exit(1)
    return razorpay.Client(auth=(key_id, key_secret))


def _run_probe(name: str, fn: Callable[[], object]) -> ProbeResult:
    try:
        fn()
        return ProbeResult(name=name, ok=True, http_status=200, error_code=None, description="reachable")
    except BadRequestError as exc:
        return ProbeResult(name=name, ok=False, http_status=400, error_code=type(exc).__name__, description=str(exc))
    except GatewayError as exc:
        return ProbeResult(name=name, ok=False, http_status=502, error_code=type(exc).__name__, description=str(exc))
    except ServerError as exc:
        return ProbeResult(name=name, ok=False, http_status=500, error_code=type(exc).__name__, description=str(exc))
    except Exception as exc:  # noqa: BLE001 — a probe's job is to record failure, not to only catch known types
        return ProbeResult(name=name, ok=False, http_status=None, error_code=type(exc).__name__, description=str(exc))


def probe_all(client: razorpay.Client) -> list[ProbeResult]:
    results: list[ProbeResult] = []

    results.append(_run_probe("orders", lambda: client.order.create({
        "amount": 100, "currency": "INR", "receipt": "probe_order_1",
    })))

    results.append(_run_probe("payment_links", lambda: client.payment_link.create({
        "amount": 100, "currency": "INR", "description": "probe",
        "customer": {"name": "Probe", "contact": "+919123456780", "email": "probe@example.com"},
    })))

    results.append(_run_probe("invoices", lambda: client.invoice.create({
        "type": "invoice", "customer": {"name": "Probe", "email": "probe@example.com"},
        "line_items": [{"name": "Probe item", "amount": 100, "currency": "INR", "quantity": 1}],
    })))

    results.append(_run_probe("customers", lambda: client.customer.create({
        "name": "Probe Customer", "email": "probe@example.com", "contact": "+919123456780",
    })))

    # plans/subscriptions/tokens_recurring/upi_autopay/emandate are the ones §5.1
    # expects to fail on an unactivated account — recorded here so the failure
    # itself (its exact code) becomes the documented evidence, not an assumption.
    plan_result_holder: dict[str, str] = {}

    def _create_plan() -> None:
        plan = client.plan.create({
            "period": "monthly", "interval": 1,
            "item": {"name": "Probe plan", "amount": 100, "currency": "INR"},
        })
        plan_result_holder["id"] = plan["id"]

    results.append(_run_probe("plans", _create_plan))

    results.append(_run_probe("subscriptions", lambda: client.subscription.create({
        "plan_id": plan_result_holder.get("id", "plan_missing_because_plan_probe_failed"),
        "customer_notify": 0, "total_count": 3,
    })))

    results.append(ProbeResult(
        name="tokens_recurring", ok=False, http_status=None, error_code="NOT_DIRECTLY_PROBEABLE",
        description="Recurring card tokenization happens through a checkout flow (recurring=1 "
                    "on an order), not a standalone server-side create call — this probe cannot "
                    "exercise it without a real checkout session. Verify manually via the "
                    "dashboard or a checkout.js test run.",
    ))
    results.append(ProbeResult(
        name="upi_autopay", ok=False, http_status=None, error_code="NOT_DIRECTLY_PROBEABLE",
        description="UPI Autopay S2S needs explicit approval and a specific subscription "
                    "auth_type — the subscriptions probe above will surface the enablement "
                    "error if the account lacks it, but a clean subscriptions success doesn't "
                    "by itself confirm UPI Autopay specifically. Verify manually.",
    ))
    results.append(ProbeResult(
        name="emandate", ok=False, http_status=None, error_code="NOT_DIRECTLY_PROBEABLE",
        description="eNACH/eMandate enablement is bank-dependent and dashboard-configured "
                    "(§5.1) — no documented standalone API call confirms it in isolation. "
                    "Verify manually via the dashboard's payment methods page.",
    ))

    results.append(_run_probe("settlements", lambda: client.settlement.all()))

    return results


def render_markdown(results: list[ProbeResult]) -> str:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# Rail Capabilities",
        "",
        f"Generated by `tools/probe_rails.py` at {now}. Re-run before submission — see DEVDOC_v6 §6.",
        "",
        "| Probe | Reachable | HTTP status | Error code | Description |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        status = "cleared" if r.ok else "blocked"
        lines.append(
            f"| `{r.name}` | {status} | {r.http_status or '—'} | {r.error_code or '—'} | "
            f"{r.description.replace(chr(10), ' ')[:200]} |"
        )
    lines.append("")
    lines.append(
        "`cleared` means the probe's call succeeded — it does not by itself prove every "
        "operation on that object works, only that creation did (§5.4's conformance scope "
        "note applies here too)."
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    client = _client()
    results = probe_all(client)
    markdown = render_markdown(results)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"\nWritten to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
