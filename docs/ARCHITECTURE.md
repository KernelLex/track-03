# Architecture

I'm describing the design in DEVDOC_v6 here, with an honest `[built]` /
`[pending]` marker on each piece — this is a design document, not a claim
that everything below is running code. See `docs/LIMITATIONS.md` for the
consolidated list of what I still have pending and why, `docs/ORCHESTRATION.md`
for the live webhook-to-action pipeline I built in detail, `docs/WHATSAPP.md`
for the WhatsApp channel, `docs/SCALABILITY.md` for the (unbuilt,
planned-only) path past a single-process/SQLite deployment, and
`docs/RESULTS.md` for the pre-registered §17 evaluation's actual numbers.

## The rail layer (§5.3)

I've marked this `[built]` and `[live-verified]` where the account allows
it, as of 2026-08-30. `agent/rails/protocol.py`'s `Rail` protocol has two
real implementations I wrote: `SimulatedRail` (`[built]`, no external
dependency) and `RazorpayRail` (`[built]`, `agent/rails/razorpay_rail.py`,
exercised against a live test-mode account in
`tests/agent/test_razorpay_rail_live.py` — skipped without credentials,
9/9 passing with them). `HybridRail` (composing the two, tagging every
call per Law 6) is `[pending]` — I don't need it yet, since I haven't
written an orchestrating pipeline that calls both rails in the same run.

I built the shared conformance suite (`agent/rails/conformance/suite.py`)
and it passes against **both** rails — the same function, not two
parallel implementations of "conforms." I've live-verified: `create_order`,
`create_payment_link`, `create_invoice`, `create_mandate` (as a
Plan+Subscription — see `docs/LIMITATIONS.md` for why that's a materially
different instrument from a variable eNACH/UPI Autopay mandate), and
`revoke_mandate`. Honestly unverified rather than guessed:
`RazorpayRail.present_debit()` and `.modify_mandate()` both raise
`RailUnavailable` — I haven't confirmed any call exists that presents an
ad-hoc debit against a Subscription on demand.

## Law 1, made concrete

> The model may SEE and SPEAK, never SPEND. No model output becomes an
> amount, a debit date, or a state transition.

Concretely, in my codebase:

| Value | Can a model produce it? |
|---|---|
| An amount in `Money`/paise that reaches a rail call | **No.** `agent/mandate/instrument.py::select_instrument()` takes a `Promise` (which *may* originate from model extraction) but the amount that reaches `SimulatedRail`/`RazorpayRail` always passes through this pure, deterministic function first — the model's number is a candidate, not the final value |
| A debit date | **No.** Same path — `Promise.installment_amount_paise` and schedules are inputs to deterministic instrument selection |
| A `DebtorState` transition | **No.** `agent/diagnose/state_machine.py::transition()` only accepts values from a fixed enum via a fixed table; nothing in that module ever calls a model |
| A fact feeding `legal_computation()` | **No, structurally.** `agent/ledger/models.py::assert_legal_provenance()` raises `ProvenanceViolation` — a crash, not a warning — the instant a `Provenance.MODEL` fact reaches it (§8) |
| A `check_bounds()` verdict | **No.** `agent/bounds/engine.py` evaluates a restricted, whitelisted expression grammar (`agent/bounds/expr.py`) against a typed context — there is no code path from "model says X" to "gate passes" |

I've marked `[built]` — provenance guard, state machine, bounds engine,
instrument selection. **Update, 2026-08-31 — I also built and
live-verified**: the actual LLM extractor (`agent/diagnose/llm_extract.py`,
DEVDOC_v6 §11.2 Path B) that *produces* `MODEL`-provenance candidates —
real Claude Sonnet 5 calls, real extractions, budget-tracked
(`agent/spend.py`) against a hard $20 ceiling. See `docs/LLM_EXTRACTION.md`.

## The seven stages + the Auditor

Per DEVDOC_v6 §10 (as amended — see DEVDOC_v6.md itself for the stage
responsibility table I added while implementing this):

| Stage | Responsibility | Status |
|---|---|---|
| `INGEST` | Verify webhook signatures, de-duplicate by `(source, event_id)` | `[built]` — `agent/ingest/webhooks.py` |
| `DIAGNOSE` | Path A structured lookup (`[built]`, `agent/diagnose/taxonomy.py`); Path B schema + objection logic + the actual model call (`[built]`, `[live-verified]` — `extract.py`/`objection.py`/`llm_extract.py`) | `[built]` |
| `DECIDE` | EV computation, `p_base` x `lift_prior` | `[built]` as pure arithmetic — `agent/decide/ev.py` (`Prior[float]`, `compute_ev()`, `decision_flips_under_perturbation()`) and `agent/decide/fitted_p_base.py` (a real fitted, holdout-evaluated model — see below). **Update, 2026-09-01**: also `[built]` end to end — `agent/orchestrate.py::run_pipeline()` calls this from a real diagnosis, live-triggered by a webhook (see the orchestration paragraph below) |
| `BOUNDS` | `check_bounds()`, the Law 3 gate | `[built]` — `agent/bounds/engine.py`, full rule register, differential test |
| `ACT` | Execute an accepted action against a `Rail` | `[built]` — `agent/act/executor.py`: `check_bounds()` first (Law 3), then a claim-then-act idempotency gate (§9.4) before ever calling the rail, dispatching `create_payment_link`, `reissue_artifact`, `create_mandate`, `retry_charge`, `initiate_refund`, `revoke_mandate`, `repair_mandate`, and message-only actions. **Law 4**: every call — accepted, refused, or deduped as a retry — writes exactly one `LedgerEntry`, including a JSON-safe `bounds_context_snapshot` (`BoundsContext.to_dict()`/`.from_dict()`) that the Auditor's bounds-integrity job later recomputes `check_bounds()` from. `ledger` is a required parameter, not optional, so there is no code path that dispatches without a ledger record. **Update, 2026-09-01**: also takes `dry_run=True` — runs the real bounds check but never claims the outbound idempotency key or calls the rail, so a whole batch of decisions can be proven with zero rupees able to move (`tools/run_dry_run_batch.py`) |
| `LISTEN` | Turn rail webhooks into `SYSTEM`-provenance facts | `[built]` — `agent/ingest/listen.py::facts_from_webhook()`, covering every event type `SimulatedRail` emits (payment captured/failed, mandate activated/revoked/notified, refund processed, link/invoice paid) |
| `SETTLE` | Attribute a `captured` payment via `recovery_ledger` | `[built]` — `agent/ledger/recovery.py`, Law 7's `UNIQUE(payment_id)` |
| `AUDITOR` *(not one of the seven)* | Extractor drift, bounds integrity, chain integrity — read-only, out-of-band | `[built]` — I wrote all three jobs in `agent/auditor/auditor.py`: chain integrity wraps `Ledger.verify_chain()`; bounds integrity recomputes `check_bounds()` from each sampled action's own recorded `bounds_context_snapshot` and raises `BoundsIntegrityBreach` on a mismatch (`tests/agent/test_auditor.py`, including a test that deliberately forges a recorded verdict and confirms it's caught). Both **run on a schedule**: `agent/auditor/scheduler.py`, APScheduler in-process (§19's stack choice), wired into `agent/api/app.py`'s lifespan behind `TRUECOMMIT_LEDGER_DB` — `uv run trucommit serve` starts both jobs alongside the webhook receiver when that env var is set, and warns loudly if it isn't (`tests/agent/test_auditor_scheduler.py`, `tests/agent/test_api_webhooks.py`). A trip logs at `CRITICAL` rather than the spec's "halt the arm," since "arm" is a concept from the eval harness (§17) — which now exists and has run once (`docs/RESULTS.md`) but as an offline comparison, not a live A/B assignment serving real traffic, so there's still nothing *live* to halt. **Update, 2026-09-01 — I also built extractor drift**: `agent/auditor/extractor_drift.py` samples logged past extractions and re-checks them against a second model, quarantining below an agreement threshold — deliberately *not* auto-scheduled like the two free jobs, since every real run spends real money against the $20 ceiling (`agent.auditor.scheduler.add_extractor_drift_job`, opt-in only) |

**Update, 2026-09-01 — I've now connected them with an orchestrator,
without violating the rule above.** `agent/orchestrate.py::run_pipeline()`,
triggered by a real webhook landing (`agent/api/app.py`), calls DIAGNOSE
(Path A) then DECIDE then BOUNDS then ACT in sequence — live-verified
against the actual running server (see `docs/ORCHESTRATION.md`). This
isn't an eighth stage calling the other seven directly: it's a driver
*outside* all of them, the same role I used to play by hand, clicking
through the demo dashboard before this existed, writing every step's
result through the same `execute_action()` ledger chokepoint every other
caller uses. No stage imports or calls another stage's module directly
anywhere in the code I've written — the pieces still communicate only by
their own pure functions being called (by the CLI, by a test, or now by
this orchestrator), which is the "ledger is the only bus" principle in
miniature, Law 4 unchanged. `agent/api/app.py` (`[built]`,
`uv run trucommit serve`) gives `INGEST`/`LISTEN` a real HTTP endpoint —
`verify_and_ingest()` then `facts_from_webhook()` on every
`POST /webhooks/{source}` — and now also triggers the orchestrator
automatically on a `payment.failed` event carrying a structured failure
code (Path A only; a live Telegram reply routing into the identical
`run_pipeline()` via Path B is the natural next step, which I haven't
built yet).

## Modules added after the seven stages (2026-09-01/02)

None of these is a stage. Each sits beside the pipeline and is called by a
driver, keeping the "no stage imports another stage" rule intact.

| Module | What it is | Why it exists |
|---|---|---|
| `agent/notify/conversation.py` | Conversation turns, the one outstanding proposal, a UNIQUE-constraint claim table for handled messages, and the dashboard's event timeline | Every reply was diagnosed standalone, so the system could make an offer and then fail to recognise the acceptance of it -- "Yes it works" scored `STALLING` at 0.15, honest calibration of a message that is genuinely ambiguous *in isolation* |
| `agent/mandate/payment_plan.py` | A debtor's own proposed split, priced and dated | "21,000 on the 5th and the rest later" is the most common useful reply in collections and the one a dunning bot handles worst |
| `agent/mandate/emandate.py` | Real, authorizable Razorpay mandates from a plan | A negotiation that ends in a polite sentence has done the hard part and dropped it |
| `agent/mandate/rail_capability.py` | §12.2's recommendation vs. what this account can actually issue | UPI Autopay is not approved here, and the demo was reporting `upi_block_reserve_pay` while creating an e-mandate |
| `agent/debtor/score.py` | `promise_credibility` from kept/broken history, and the published bands it earns | The bounds gate has scaled `PROMISE_COOLDOWN` by this value since it was written, and nothing ever computed it -- every context used the `1.0` default |
| `agent/debtor/invoices.py` | A debtor's invoices and what has happened to each | The conversation was about one hardcoded invoice, so "which of these do I still owe" could not be asked at all |
| `agent/notify/intents.py` | The handful of things a debtor says that are commands, not prose | "2" and "dispute" need no language model, and routing them through one costs four seconds and risks reading them as something they did not ask for |
| `agent/clock.py` | What "today" means to an Indian business | `date.today()` is UTC on Render, so for five and a half hours a day every relative date resolved one day early |
| `agent/debtor/registry.py` | Who owes what, and every promise behind their score | There was no way to ask "what has this debtor done before", which is the question `promise_credibility` was designed around |

**Path B now routes into the live conversation.** The paragraph above ends
by calling a live Telegram reply through Path B "the natural next step,
which I haven't built yet". It is built:
`POST /demo/telegram-webhook` authenticates Telegram's own `secret_token`,
then `handle_inbound_message()` runs the real extractor, the real
`select_action_for_diagnosis()` -> `check_bounds()` decision, and the real
composer. It is still a driver outside the stages, calling their pure
functions, not a stage calling another stage.

**A refusal is now visible, not just recorded.** `_decide_next_step()`
returns the refusing rules' own `human` text and a passed/total tally
alongside the chosen action, so the dashboard can render the gate's verdict
as a state rather than a sentence (`docs/DEMO_UI.md`). No new decision
logic -- the reason was already on `BoundsVerdict` and was being discarded
at the API boundary.

**A capture now moves a debtor's record.** `payment.captured` settles the
oldest open promise through the same webhook that INGEST and SETTLE already
handle -- deliberately driven from the rail's event rather than from
anything said in conversation, since Law 7's standard is a confirmed
capture and a score built on anything softer would be a score built on how
convincing someone sounded.

## Path B's extraction schema (§11.2)

I built this `[built]` as a schema and validation boundary —
`agent/diagnose/extract.py`, a Pydantic model matching DEVDOC_v6's JSON
shape exactly (`family`, `class`, `confidence`, `promise`, `dispute`,
`entities`, `objection_signal`), with `extra="forbid"` so a
schema-poisoning attempt to smuggle in an unlisted field (e.g.
`"state": "RECOVERED"`) gets rejected outright rather than silently
ignored. Every field's provenance is `MODEL` (§8) by construction — I
never label an `ExtractionResult` `SYSTEM` or `HUMAN` anywhere in that
module — consistent with how I built `agent/mandate/instrument.py::Promise`
to accept exactly this kind of untrusted candidate without ever becoming
the final number itself.

**Update, 2026-08-31 — I built and live-verified**: the actual model call
that *produces* an `ExtractionResult` from raw debtor text —
`agent/diagnose/llm_extract.py::extract_from_reply()`, real Claude Sonnet
5 calls via structured output (`client.messages.parse()`), so every
existing validator on this schema runs on the model's real output exactly
as it does on a hand-built test object I wrote.
`tests/agent/test_injection_resistance.py` still exercises the schema and
the family/class -> action-set mapping against worst-case
hand-constructed `ExtractionResult` instances I built to stand in for
"whatever a compromised model might output" — a structural test, not an
empirical one, and still the right test even now that a real model call
exists (a live model could in principle produce any output the schema
permits, so the worst-case-schema test remains the actual safety
argument); see `docs/THREAT_MODEL.md` for exactly what it does and
doesn't prove, and `docs/LLM_EXTRACTION.md` for the live-verified results.

§8's deemed-acceptance worked example (model as veto only, never as
establisher of fact) I built independently of the extractor itself —
`agent/diagnose/objection.py`'s `compute_objection_marker` and
`compute_deemed_acceptance` take an `ExtractionResult` as input, so
they're fully testable today with hand-built extractions and become live
the moment a real extractor produces one.

## Three families and their action sets (§11.2)

| Family | Meaning | Actions unlocked | Implemented? |
|---|---|---|---|
| A — Instrument failure | Money exists, rail failed/will fail | `retry_charge`, `repair_mandate`, `create_payment_link` | Diagnosis: `[built]` (`agent/diagnose/taxonomy.py`). Actions: typed and **orchestrated live** (`agent/orchestrate.py` selects `create_payment_link` from a real webhook failure code; `repair_mandate` is orchestrated too, against `SimulatedRail`, via `agent/mandate/lifecycle.py`'s detect-repair-notify-present flow) |
| B — Administrative blocker | Money + willingness exist, paperwork blocks | `reissue_artifact`, `request_reconciliation` | Orchestrated live via `agent/orchestrate.py` (webhook-triggered) and proven against the real Razorpay rail (`tools/run_real_scenarios.py`) |
| C — Liquidity/willingness | Money isn't there or won't be released | `create_mandate`, `create_payment_link`, `send_reminder` | `select_instrument()` `[built]`; a real `create_mandate` call is live-verified (`tools/run_real_scenarios.py`, a real Subscription with a real customer-facing link) and an early-payment-discount variant of `create_payment_link` exists (`agent/mandate/early_payment.py`) |
| D — Dispute | Contested obligation | `escalate_human` only | State machine handles `DISPUTED_FROZEN` `[built]`; the schema + action-set mapping is `[built]` and proven to never unlock anything beyond `escalate_human` for any family-D class (`tests/agent/test_injection_resistance.py`); Path A (structured codes) produces live family-D-shaped orchestration decisions today, proven live including a real `NO_MANDATE_ON_DISPUTE` refusal (`tools/run_real_scenarios.py`) — Path B (a live model reading free debtor text) does not yet feed a family-D diagnosis into the orchestrator (see the DIAGNOSE row above) |

## Debtor state machine (§11.3)

I built this in `agent/diagnose/state_machine.py`. 14 states, and I
exhaustively tested it over the full 14x14 transition Cartesian product
(`tests/agent/test_state_machine.py`, 201 cases). Terminal states —
derived from the transition table itself, not a separately maintained
list, so they can't drift apart — are exactly `RECOVERED` and `EXHAUSTED`.
`DISPUTED_FROZEN` is *not* fully terminal: DEVDOC_v6 §24.2's CLOCK
amendment gives it one legal exit, to `HUMAN_QUEUE`, once an unsubstantiated
dispute's window elapses.

## The EV gate (§11.4)

I built the arithmetic and both halves' typing:

```python
ev_paise = int(p_base * lift_prior * recoverable_paise) - cost_paise(action)
#          ^^^^^^ fitted, calibrated   ^^^^^^^^^^ a declared prior, typed Prior[float]
```

In `agent/decide/ev.py`: I wrapped `lift_prior` in a real `Prior` class
(`isinstance`-checkable, not a `NewType` fiction that vanishes at
runtime), `compute_ev()` raises `TypeError` if handed a bare `float` for
it, and `decision_flips_under_perturbation()` implements §17.5's
flip-rate methodology for one `(p_base, lift_prior)` cell at a time.

`p_base` **is fitted** — `agent/decide/fitted_p_base.py` loads real
coefficients from `data/fitted_params.yaml`, which I produced with
`tools/fit_persona_params.py`, fitting a logistic regression against the
Kaggle Payment Date Prediction dataset (50,000 rows, committed in
`data/ar_seed/`), evaluated on an 8,000-row holdout (Brier score 0.0206,
reliability-diagram data included). See `docs/LIMITATIONS.md` for the
honest caveat: the dataset's 97.9% base rate means this is a
well-calibrated but weakly discriminative fit, not a strong predictive
signal — real evidence, correctly scoped, not a claim of more than it
shows.

The bounds engine's `EV_FLOOR` rule (`agent/bounds/rules.yaml`) already
checks `decision.ev_paise > 0` structurally. **Update, 2026-09-01 — I
built**: `agent/orchestrate.py::run_pipeline()`, which orchestrates
DIAGNOSE's output into `compute_ev()`'s inputs to produce a real
`Decision` end to end, live-verified against a real webhook. I've also
done §17's own evaluation of this arithmetic in aggregate (over a
synthetic population, not live traffic) — see `docs/RESULTS.md`.

## Typed actions and their Razorpay objects (§11.5)

I built this as metadata — `agent/act/actions.py`. See the generated
"Escalation ladder" table in `docs/BOUNDS.md` for the full action-by-action
breakdown, which I produce from this same module so it can't drift from it.

## Reversal (§11.6, Law 9)

I built this in `agent/act/reversal.py`. `REVERSAL_MAP` names every
money-moving action's inverse (`retry_charge` -> `initiate_refund`,
`create_mandate` -> `revoke_mandate`, `reissue_artifact` -> itself with
prior corrections, `send_statutory_notice` -> `send_correction_notice`),
each tagged `HUMAN`- or `AUTONOMOUS`-gated — `revoke_mandate` on a
debtor's own opt-out is the one case I made must stay autonomous, since
refusing to honour an opt-out would itself be a violation. A reversal
writes its own row (never netted silently into a recovered total) and
carries `reverses_seq`, pointing at the original action's ledger entry, so
`replay()` reconstructs both the error and the correction from the ledger
alone. `INITIATE_REFUND`/`REVOKE_MANDATE` dispatch through the same
`agent/act/executor.py` chokepoint as every other action (§11.5's table),
gated by the real `REFUND_AND_REVOKE_HUMAN_GATE` bounds rule.

## §9 — money, idempotency, webhook integrity

I built and tested all three subsections:

- **Money** (§9.1): `agent/money.py`, `Money = NewType("Money", int)`, a
  runtime `assert_money()` guard. I haven't implemented a *static* lint
  rule against float arithmetic on `Money` (would need a custom mypy/ruff
  plugin) — I'm noting that honestly, not silently skipping it.
- **Signature verification** (§9.2): `agent/rails/webhook_signing.py`,
  HMAC-SHA256, `hmac.compare_digest`, one function I use for both
  `SimulatedRail`'s emission and `agent/ingest/webhooks.py`'s verification.
- **Idempotency, all three defenses** (§9.3): redelivery
  (`EventStore`, `UNIQUE(source, event_id)`), out-of-order arrival (state
  guards are order-tolerant by construction — the debtor state machine
  raises on an illegal transition rather than assuming arrival order),
  double attribution (`RecoveryLedger`, `UNIQUE(payment_id)`).
- **Outbound idempotency** (§9.4): I built `agent/act/executor.py`'s
  `OutboundActionStore`, keyed by `(debtor_id, invoice_id, action_type,
  decision_seq)`, using claim-then-act (an atomic `INSERT` under a
  `UNIQUE` constraint) rather than check-then-act, so a concurrent or
  retried dispatch of the same key can't reach the rail twice.
- **The shuffled-thrice test** (§9.5): built and passing —
  `tests/agent/test_shuffled_replay.py` runs the real `verify_and_ingest`
  -> `RecoveryLedger.attribute` pipeline three times with a shuffled,
  redelivery-duplicated event stream and asserts identical totals.

## Degradation (§28)

**Update, 2026-09-01**: I now have a real, running service to degrade
(`uv run trucommit serve`, the orchestrator, the scheduled Auditor), so
this is no longer purely a claim about properties of pieces that don't
run together yet. The properties that make each failure mode safe: Family
A diagnosis never touches a model (`agent/diagnose/taxonomy.py` has no
model dependency at all, and my live webhook orchestrator uses only Path
A today, so "LLM unavailable" degrades to exactly this already-live path
with zero code change needed); `check_bounds()` fails closed (an
exception during evaluation propagates rather than defaulting to PASS);
the ledger's chain-integrity check refuses to silently continue past a
broken hash (`verify_chain()` raises, naming the exact `seq`); a
`dry_run=True` call (`agent/act/executor.py`) is the same "fail toward
doing nothing" pattern I applied deliberately, not just as a failure
response — it lets a whole batch of decisions be proven with zero rupees
able to move (`tools/run_dry_run_batch.py`).
