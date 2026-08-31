# Architecture

This describes the design in DEVDOC_v6, with an honest `[built]` / `[pending]`
marker on each piece — this is a design document, not a claim that
everything below is running code. See `docs/LIMITATIONS.md` for the
consolidated list of what's pending and why, `docs/ORCHESTRATION.md` for
the live webhook-to-action pipeline in detail, `docs/WHATSAPP.md` for the
WhatsApp channel, `docs/SCALABILITY.md` for the (unbuilt, planned-only)
path past a single-process/SQLite deployment, and `docs/RESULTS.md` for
the pre-registered §17 evaluation's actual numbers.

## The rail layer (§5.3)

`[built]` and `[live-verified]` where the account allows it, as of
2026-08-30. `agent/rails/protocol.py`'s `Rail` protocol has two real
implementations: `SimulatedRail` (`[built]`, no external dependency) and
`RazorpayRail` (`[built]`, `agent/rails/razorpay_rail.py`, exercised
against a live test-mode account in `tests/agent/test_razorpay_rail_live.py`
— skipped without credentials, 9/9 passing with them). `HybridRail`
(composing the two, tagging every call per Law 6) is `[pending]` — nothing
needs it yet since no orchestrating pipeline calls both rails in the same
run.

The shared conformance suite (`agent/rails/conformance/suite.py`) is
`[built]` and passes against **both** rails — the same function, not two
parallel implementations of "conforms." Live-verified: `create_order`,
`create_payment_link`, `create_invoice`, `create_mandate` (as a
Plan+Subscription — see `docs/LIMITATIONS.md` for why that's a materially
different instrument from a variable eNACH/UPI Autopay mandate), and
`revoke_mandate`. Honestly unverified rather than guessed:
`RazorpayRail.present_debit()` and `.modify_mandate()` both raise
`RailUnavailable` — no call this build has confirmed exists presents an
ad-hoc debit against a Subscription on demand.

## Law 1, made concrete

> The model may SEE and SPEAK, never SPEND. No model output becomes an
> amount, a debit date, or a state transition.

Concretely, in this codebase:

| Value | Can a model produce it? |
|---|---|
| An amount in `Money`/paise that reaches a rail call | **No.** `agent/mandate/instrument.py::select_instrument()` takes a `Promise` (which *may* originate from model extraction) but the amount that reaches `SimulatedRail`/`RazorpayRail` always passes through this pure, deterministic function first — the model's number is a candidate, not the final value |
| A debit date | **No.** Same path — `Promise.installment_amount_paise` and schedules are inputs to deterministic instrument selection |
| A `DebtorState` transition | **No.** `agent/diagnose/state_machine.py::transition()` only accepts values from a fixed enum via a fixed table; nothing in that module ever calls a model |
| A fact feeding `legal_computation()` | **No, structurally.** `agent/ledger/models.py::assert_legal_provenance()` raises `ProvenanceViolation` — a crash, not a warning — the instant a `Provenance.MODEL` fact reaches it (§8) |
| A `check_bounds()` verdict | **No.** `agent/bounds/engine.py` evaluates a restricted, whitelisted expression grammar (`agent/bounds/expr.py`) against a typed context — there is no code path from "model says X" to "gate passes" |

`[built]` — provenance guard, state machine, bounds engine, instrument
selection. **Update, 2026-08-31 — also built and live-verified**: the
actual LLM extractor (`agent/diagnose/llm_extract.py`, DEVDOC_v6 §11.2
Path B) that *produces* `MODEL`-provenance candidates — real Claude Sonnet
5 calls, real extractions, budget-tracked (`agent/spend.py`) against a
hard $20 ceiling. See `docs/LLM_EXTRACTION.md`.

## The seven stages + the Auditor

Per DEVDOC_v6 §10 (as amended — see DEVDOC_v6.md itself for the stage
responsibility table added while implementing this):

| Stage | Responsibility | Status |
|---|---|---|
| `INGEST` | Verify webhook signatures, de-duplicate by `(source, event_id)` | `[built]` — `agent/ingest/webhooks.py` |
| `DIAGNOSE` | Path A structured lookup (`[built]`, `agent/diagnose/taxonomy.py`); Path B schema + objection logic + the actual model call (`[built]`, `[live-verified]` — `extract.py`/`objection.py`/`llm_extract.py`) | `[built]` |
| `DECIDE` | EV computation, `p_base` x `lift_prior` | `[built]` as pure arithmetic — `agent/decide/ev.py` (`Prior[float]`, `compute_ev()`, `decision_flips_under_perturbation()`) and `agent/decide/fitted_p_base.py` (a real fitted, holdout-evaluated model — see below). **Update, 2026-09-01**: also `[built]` end to end — `agent/orchestrate.py::run_pipeline()` calls this from a real diagnosis, live-triggered by a webhook (see the orchestration paragraph below) |
| `BOUNDS` | `check_bounds()`, the Law 3 gate | `[built]` — `agent/bounds/engine.py`, full rule register, differential test |
| `ACT` | Execute an accepted action against a `Rail` | `[built]` — `agent/act/executor.py`: `check_bounds()` first (Law 3), then a claim-then-act idempotency gate (§9.4) before ever calling the rail, dispatching `create_payment_link`, `reissue_artifact`, `create_mandate`, `retry_charge`, `initiate_refund`, `revoke_mandate`, `repair_mandate`, and message-only actions. **Law 4**: every call — accepted, refused, or deduped as a retry — writes exactly one `LedgerEntry`, including a JSON-safe `bounds_context_snapshot` (`BoundsContext.to_dict()`/`.from_dict()`) that the Auditor's bounds-integrity job later recomputes `check_bounds()` from. `ledger` is a required parameter, not optional, so there is no code path that dispatches without a ledger record. **Update, 2026-09-01**: also takes `dry_run=True` — runs the real bounds check but never claims the outbound idempotency key or calls the rail, so a whole batch of decisions can be proven with zero rupees able to move (`tools/run_dry_run_batch.py`) |
| `LISTEN` | Turn rail webhooks into `SYSTEM`-provenance facts | `[built]` — `agent/ingest/listen.py::facts_from_webhook()`, covering every event type `SimulatedRail` emits (payment captured/failed, mandate activated/revoked/notified, refund processed, link/invoice paid) |
| `SETTLE` | Attribute a `captured` payment via `recovery_ledger` | `[built]` — `agent/ledger/recovery.py`, Law 7's `UNIQUE(payment_id)` |
| `AUDITOR` *(not one of the seven)* | Extractor drift, bounds integrity, chain integrity — read-only, out-of-band | `[built]`, all three jobs — `agent/auditor/auditor.py`: chain integrity wraps `Ledger.verify_chain()`; bounds integrity recomputes `check_bounds()` from each sampled action's own recorded `bounds_context_snapshot` and raises `BoundsIntegrityBreach` on a mismatch (`tests/agent/test_auditor.py`, including a test that deliberately forges a recorded verdict and confirms it's caught). Both **run on a schedule**: `agent/auditor/scheduler.py`, APScheduler in-process (§19's stack choice), wired into `agent/api/app.py`'s lifespan behind `TRUECOMMIT_LEDGER_DB` — `uv run trucommit serve` starts both jobs alongside the webhook receiver when that env var is set, and warns loudly if it isn't (`tests/agent/test_auditor_scheduler.py`, `tests/agent/test_api_webhooks.py`). A trip logs at `CRITICAL` rather than the spec's "halt the arm," since "arm" is a concept from the eval harness (§17) — which now exists and has run once (`docs/RESULTS.md`) but as an offline comparison, not a live A/B assignment serving real traffic, so there's still nothing *live* to halt. **Update, 2026-09-01 — extractor drift is also built**: `agent/auditor/extractor_drift.py` samples logged past extractions and re-checks them against a second model, quarantining below an agreement threshold — deliberately *not* auto-scheduled like the two free jobs, since every real run spends real money against the $20 ceiling (`agent.auditor.scheduler.add_extractor_drift_job`, opt-in only) |

**Update, 2026-09-01 — an orchestrator now connects them, without violating
the rule above.** `agent/orchestrate.py::run_pipeline()`, triggered by a
real webhook landing (`agent/api/app.py`), calls DIAGNOSE (Path A) then
DECIDE then BOUNDS then ACT in sequence — live-verified against the actual
running server (see `docs/ORCHESTRATION.md`). This is not an eighth stage
calling the other seven directly: it's a driver *outside* all of them, the
same role a human clicking through the demo dashboard played before this
existed, writing every step's result through the same `execute_action()`
ledger chokepoint every other caller uses. No stage imports or calls
another stage's module directly anywhere in the code that exists — the
pieces still communicate only by their own pure functions being called (by
the CLI, by a test, or now by this orchestrator), which is the "ledger is
the only bus" principle in miniature, Law 4 unchanged. `agent/api/app.py`
(`[built]`, `uv run trucommit serve`) gives `INGEST`/`LISTEN` a real HTTP
endpoint — `verify_and_ingest()` then `facts_from_webhook()` on every
`POST /webhooks/{source}` — and now also triggers the orchestrator
automatically on a `payment.failed` event carrying a structured failure
code (Path A only; a live Telegram reply routing into the identical
`run_pipeline()` via Path B is the natural next step, not built).

## Path B's extraction schema (§11.2)

`[built]` as a schema and validation boundary — `agent/diagnose/extract.py`,
a Pydantic model matching DEVDOC_v6's JSON shape exactly (`family`, `class`,
`confidence`, `promise`, `dispute`, `entities`, `objection_signal`), with
`extra="forbid"` so a schema-poisoning attempt to smuggle in an unlisted
field (e.g. `"state": "RECOVERED"`) is rejected outright rather than
silently ignored. Every field's provenance is `MODEL` (§8) by construction
— nothing in this module ever labels an `ExtractionResult` `SYSTEM` or
`HUMAN` — consistent with how `agent/mandate/instrument.py::Promise` is
built to accept exactly this kind of untrusted candidate without ever
becoming the final number itself.

**Update, 2026-08-31 — built and live-verified**: the actual model call
that *produces* an `ExtractionResult` from raw debtor text —
`agent/diagnose/llm_extract.py::extract_from_reply()`, real Claude Sonnet
5 calls via structured output (`client.messages.parse()`), so every
existing validator on this schema runs on the model's real output exactly
as it does on a hand-built test object. `tests/agent/test_injection_resistance.py`
still exercises the schema and the family/class -> action-set mapping
against worst-case hand-constructed `ExtractionResult` instances standing
in for "whatever a compromised model might output" — a structural test,
not an empirical one, and still the right test even now that a real model
call exists (a live model could in principle produce any output the
schema permits, so the worst-case-schema test remains the actual safety
argument); see `docs/THREAT_MODEL.md` for exactly what it does and doesn't
prove, and `docs/LLM_EXTRACTION.md` for the live-verified results.

§8's deemed-acceptance worked example (model as veto only, never as
establisher of fact) is `[built]` independently of the extractor itself —
`agent/diagnose/objection.py`'s `compute_objection_marker` and
`compute_deemed_acceptance` take an `ExtractionResult` as input, so they're
fully testable today with hand-built extractions and become live the
moment a real extractor produces one.

## Three families and their action sets (§11.2)

| Family | Meaning | Actions unlocked | Implemented? |
|---|---|---|---|
| A — Instrument failure | Money exists, rail failed/will fail | `retry_charge`, `repair_mandate`, `create_payment_link` | Diagnosis: `[built]` (`agent/diagnose/taxonomy.py`). Actions: typed and **orchestrated live** (`agent/orchestrate.py` selects `create_payment_link` from a real webhook failure code; `repair_mandate` is orchestrated too, against `SimulatedRail`, via `agent/mandate/lifecycle.py`'s detect-repair-notify-present flow) |
| B — Administrative blocker | Money + willingness exist, paperwork blocks | `reissue_artifact`, `request_reconciliation` | Orchestrated live via `agent/orchestrate.py` (webhook-triggered) and proven against the real Razorpay rail (`tools/run_real_scenarios.py`) |
| C — Liquidity/willingness | Money isn't there or won't be released | `create_mandate`, `create_payment_link`, `send_reminder` | `select_instrument()` `[built]`; a real `create_mandate` call is live-verified (`tools/run_real_scenarios.py`, a real Subscription with a real customer-facing link) and an early-payment-discount variant of `create_payment_link` exists (`agent/mandate/early_payment.py`) |
| D — Dispute | Contested obligation | `escalate_human` only | State machine handles `DISPUTED_FROZEN` `[built]`; the schema + action-set mapping is `[built]` and proven to never unlock anything beyond `escalate_human` for any family-D class (`tests/agent/test_injection_resistance.py`); Path A (structured codes) produces live family-D-shaped orchestration decisions today, proven live including a real `NO_MANDATE_ON_DISPUTE` refusal (`tools/run_real_scenarios.py`) — Path B (a live model reading free debtor text) does not yet feed a family-D diagnosis into the orchestrator (see the DIAGNOSE row above) |

## Debtor state machine (§11.3)

`[built]`, `agent/diagnose/state_machine.py`. 14 states, exhaustively
tested over the full 14x14 transition Cartesian product
(`tests/agent/test_state_machine.py`, 201 cases). Terminal states —
derived from the transition table itself, not a separately maintained
list, so they can't drift apart — are exactly `RECOVERED` and `EXHAUSTED`.
`DISPUTED_FROZEN` is *not* fully terminal: DEVDOC_v6 §24.2's CLOCK
amendment gives it one legal exit, to `HUMAN_QUEUE`, once an unsubstantiated
dispute's window elapses.

## The EV gate (§11.4)

`[built]`, the arithmetic and both halves' typing:

```python
ev_paise = int(p_base * lift_prior * recoverable_paise) - cost_paise(action)
#          ^^^^^^ fitted, calibrated   ^^^^^^^^^^ a declared prior, typed Prior[float]
```

`agent/decide/ev.py`: `lift_prior` is wrapped in a real `Prior` class
(`isinstance`-checkable, not a `NewType` fiction that vanishes at runtime),
`compute_ev()` raises `TypeError` if handed a bare `float` for it, and
`decision_flips_under_perturbation()` implements §17.5's flip-rate
methodology for one `(p_base, lift_prior)` cell at a time.

`p_base` **is fitted** — `agent/decide/fitted_p_base.py` loads real
coefficients from `data/fitted_params.yaml`, produced by
`tools/fit_persona_params.py` fitting a logistic regression against the
Kaggle Payment Date Prediction dataset (50,000 rows, committed in
`data/ar_seed/`), evaluated on an 8,000-row holdout (Brier score 0.0206,
reliability-diagram data included). See `docs/LIMITATIONS.md` for the
honest caveat: the dataset's 97.9% base rate means this is a well-calibrated
but weakly discriminative fit, not a strong predictive signal — real
evidence, correctly scoped, not a claim of more than it shows.

The bounds engine's `EV_FLOOR` rule (`agent/bounds/rules.yaml`) already
checks `decision.ev_paise > 0` structurally. **Update, 2026-09-01 —
built**: `agent/orchestrate.py::run_pipeline()` orchestrates DIAGNOSE's
output into `compute_ev()`'s inputs to produce a real `Decision` end to
end, live-verified against a real webhook. §17's own evaluation of this
arithmetic in aggregate (over a synthetic population, not live traffic)
is also done — see `docs/RESULTS.md`.

## Typed actions and their Razorpay objects (§11.5)

`[built]` as metadata — `agent/act/actions.py`. See the generated
"Escalation ladder" table in `docs/BOUNDS.md` for the full action-by-action
breakdown, which is produced from this same module so it can't drift from it.

## Reversal (§11.6, Law 9)

`[built]` — `agent/act/reversal.py`. `REVERSAL_MAP` names every
money-moving action's inverse (`retry_charge` -> `initiate_refund`,
`create_mandate` -> `revoke_mandate`, `reissue_artifact` -> itself with
prior corrections, `send_statutory_notice` -> `send_correction_notice`),
each tagged `HUMAN`- or `AUTONOMOUS`-gated — `revoke_mandate` on a
debtor's own opt-out is the one case that must stay autonomous, since
refusing to honour an opt-out would itself be a violation. A reversal
writes its own row (never netted silently into a recovered total) and
carries `reverses_seq`, pointing at the original action's ledger entry, so
`replay()` reconstructs both the error and the correction from the ledger
alone. `INITIATE_REFUND`/`REVOKE_MANDATE` dispatch through the same
`agent/act/executor.py` chokepoint as every other action (§11.5's table),
gated by the real `REFUND_AND_REVOKE_HUMAN_GATE` bounds rule.

## §9 — money, idempotency, webhook integrity

All three subsections are `[built]` and tested:

- **Money** (§9.1): `agent/money.py`, `Money = NewType("Money", int)`, a
  runtime `assert_money()` guard. A *static* lint rule against float
  arithmetic on `Money` is not implemented (would need a custom mypy/ruff
  plugin) — noted honestly, not silently skipped.
- **Signature verification** (§9.2): `agent/rails/webhook_signing.py`,
  HMAC-SHA256, `hmac.compare_digest`, one function used by both
  `SimulatedRail`'s emission and `agent/ingest/webhooks.py`'s verification.
- **Idempotency, all three defenses** (§9.3): redelivery
  (`EventStore`, `UNIQUE(source, event_id)`), out-of-order arrival (state
  guards are order-tolerant by construction — the debtor state machine
  raises on an illegal transition rather than assuming arrival order),
  double attribution (`RecoveryLedger`, `UNIQUE(payment_id)`).
- **Outbound idempotency** (§9.4): `[built]` — `agent/act/executor.py`'s
  `OutboundActionStore`, keyed by `(debtor_id, invoice_id, action_type,
  decision_seq)`, using claim-then-act (an atomic `INSERT` under a `UNIQUE`
  constraint) rather than check-then-act, so a concurrent or retried
  dispatch of the same key can't reach the rail twice.
- **The shuffled-thrice test** (§9.5): `[built]` and passing —
  `tests/agent/test_shuffled_replay.py` runs the real `verify_and_ingest`
  -> `RecoveryLedger.attribute` pipeline three times with a shuffled,
  redelivery-duplicated event stream and asserts identical totals.

## Degradation (§28)

**Update, 2026-09-01**: a real, running service now exists to degrade
(`uv run trucommit serve`, the orchestrator, the scheduled Auditor), so
this is no longer purely a claim about properties of pieces that don't run
together yet. The properties that make each failure mode safe: Family A
diagnosis never touches a model (`agent/diagnose/taxonomy.py` has no model
dependency at all, and the live webhook orchestrator uses only Path A
today, so "LLM unavailable" degrades to exactly this already-live path
with zero code change needed); `check_bounds()` fails closed (an exception
during evaluation propagates rather than defaulting to PASS); the ledger's
chain-integrity check refuses to silently continue past a broken hash
(`verify_chain()` raises, naming the exact `seq`); a `dry_run=True` call
(`agent/act/executor.py`) is the same "fail toward doing nothing" pattern
applied deliberately, not just as a failure response — it lets a whole
batch of decisions be proven with zero rupees able to move
(`tools/run_dry_run_batch.py`).
