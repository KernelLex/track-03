# Auto-orchestration

The gap I'm closing here: until now, every action needed a manual call or
a demo dashboard click. `agent/orchestrate.py` + the wiring I added in
`agent/api/app.py` make DIAGNOSE → DECIDE → BOUNDS → ACT run
automatically, triggered by a real webhook landing — nobody clicking
anything.

## Does this violate Law 4?

No. Law 4 says *"Agents coordinate only through the ledger. No stage
calls another."* The orchestrator isn't a ninth agent that other agents
call — it's the driver *outside* all of them, the same role I was playing
by hand, clicking through the demo dashboard, until now. It calls each
agent's own already-tested public function in sequence and writes every
step's result to the real ledger through the same `execute_action()`
chokepoint everything else uses. Diagnose still doesn't know Decide
exists; Decide still doesn't know Bounds exists.

## What actually happens

1. A real `payment.failed` webhook arrives at `POST /webhooks/{source}`
   (signature-verified, deduplicated — unchanged from before).
2. `facts_from_webhook()` extracts `payment_failure_code` (unchanged).
3. **New**: if a failure code is present, `_maybe_orchestrate()` runs:
   - `diagnose_from_failure_code()` — Path A, no model, maps the real
     Razorpay error code (`data/failure_taxonomy.yaml`) to a
     `DiagnosisClass`. Every one of the 20 real codes is mapped and tested
     for coverage (`tests/agent/test_orchestrate.py`).
   - `select_action_for_diagnosis()` picks an action always drawn from
     `ACTIONS_UNLOCKED[family]` (asserted at import time).
   - `compute_ev()` — the real fitted `p_base` model, real EV arithmetic.
   - `check_bounds()` — the real gate, built from the debtor's actual
     ledger history (`touches_last_7_days()`, `Ledger.replay().current_state`),
     not a hand-typed context.
   - `execute_action()` — the real chokepoint, same idempotency and ledger
     write as every other action in my project.
4. **New**: if the action created a payment link or invoice, a second real
   send follows — the debtor is actually told about it via Telegram, with
   the real `short_url` the rail returned. `CREATE_PAYMENT_LINK` isn't a
   `MESSAGE_ONLY_ACTION`, so ACT alone doesn't notify anyone; without this,
   a real link would get created and nobody would ever see it.

## A real design decision I made while building this

`RETRY_CHARGE` calls `rail.present_debit(mandate_id, ...)` — a real,
*existing* mandate. A plain `payment.failed` webhook carries no
`mandate_id` fact (it's not necessarily from a subscription at all), so
there's no real mandate to retry against. `select_action_for_diagnosis()`
does have a `disposition`-aware branch that would pick `RETRY_CHARGE` for
a RETRYABLE code — I deliberately leave it unused by the webhook path,
which passes no disposition and always gets `CREATE_PAYMENT_LINK`
instead. I found this live, not by inspection: an early version of mine
passed `disposition_for_code()` straight through and the very first real
test crashed inside `SimulatedRail.present_debit()` on a nonexistent
mandate. I keep `RETRY_CHARGE` real and correct for a caller that
actually has mandate context (a mandate-health sweep, say) — not for this
one.

## Known simplifications, stated plainly

- **`debtor_id`/`invoice_id` are derived from the payment id**
  (`debtor_{payment_id}`), not looked up from a real merchant AR system —
  I don't have such a system connected. A real deployment would read them
  from the payment's `notes` field (Razorpay supports arbitrary
  merchant-supplied metadata there) instead.
- **Path A only.** Path B (a live Claude call reading a free-text reply)
  produces the identical `ExtractionResult` shape `run_pipeline()` expects
  — wiring a live Telegram reply into this orchestrator is the natural
  next step for me, not a redesign of anything here.
- **`SimulatedRail` by default** for the auto-triggered path. A real
  `RazorpayRail` would create a real object in the merchant's account on
  every single test webhook, the wrong default for a dev/demo server. I
  use `TRUECOMMIT_ORCHESTRATOR_RAIL=razorpay` deliberately to switch to
  the real one.
- **Touch cost is a flat constant** (`DEFAULT_TOUCH_COST_PAISE`), not yet
  per-channel (Telegram ~free, a call has real cost) — a known
  simplification I made, not an oversight.

## Live-verified, 2026-09-01

I POSTed a real signed `payment.failed` webhook to the actual running
server (not a test client), and it produced:

```json
{
  "status": "ingested",
  "orchestration": {
    "diagnosis": {"family": "A", "class": "INSUFFICIENT_FUNDS"},
    "action_type": "create_payment_link",
    "bounds_passed": true,
    "external_ref": "plink_0234bb0863490a",
    "notified": true
  }
}
```

`notified: true` means a real Telegram message with the real payment link
went out, automatically, with nobody clicking anything between the webhook
arriving and the message sending.

## Testing

- `tests/agent/test_orchestrate.py` (19 tests) — I test the orchestrator
  module in isolation: every real taxonomy code maps to a class, action
  selection, touch counting, and `run_pipeline()` against a real
  `SimulatedRail` and real `Ledger`, including a test I wrote that runs
  the same debtor through the pipeline repeatedly until a real bounds
  rule refuses it (not rigged by hand).
- `tests/agent/test_api_orchestration.py` (8 tests) — I test the actual
  HTTP wiring: a signed webhook through FastAPI's real request/response
  cycle triggers orchestration, writes to the real ledger, doesn't
  double-fire on a redelivery, and doesn't 500 on an unmappable code.

## SETTLE, 2026-09-01: the pipeline no longer stops at ACT

The unattended path described above ran DIAGNOSE -> DECIDE -> BOUNDS -> ACT
and stopped. A `payment.captured` landing on the same endpoint was ingested
and then dropped, because the orchestrator only ever looked for a failure
code — so nothing in the live path could ever record money as recovered.

`_maybe_settle` closes that. It calls the same
`RecoveryLedger.attribute()` (§16, Law 7) every other caller uses, so the
three properties that make attribution mean anything are enforced in the
database rather than here: only a `captured` status is attributable, the
`UNIQUE(payment_id)` constraint decides duplicates rather than application
logic, and the entry carries `rail_tag="razorpay"` so a real capture can
never be confused with a simulated one (Law 6).

ids come from the payment's own `notes` when the merchant set them, then
the real `invoice_id` an invoice payment carries, and only then a derived
placeholder — which is the honest answer to the placeholder-id limitation
this document has carried since it was written.
