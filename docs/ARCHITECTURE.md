# Architecture

This describes the design in DEVDOC_v6, with an honest `[built]` / `[pending]`
marker on each piece — this is a design document, not a claim that
everything below is running code. See `docs/LIMITATIONS.md` for the
consolidated list of what's pending and why.

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
selection. `[pending]` — the actual LLM extractor (`diagnose/extract.py` in
DEVDOC_v6 §11.2 Path B) that would *produce* `MODEL`-provenance candidates
in the first place; nothing calls an LLM anywhere in this codebase yet.

## The seven stages + the Auditor

Per DEVDOC_v6 §10 (as amended — see DEVDOC_v6.md itself for the stage
responsibility table added while implementing this):

| Stage | Responsibility | Status |
|---|---|---|
| `INGEST` | Verify webhook signatures, de-duplicate by `(source, event_id)` | `[built]` — `agent/ingest/webhooks.py` |
| `DIAGNOSE` | Path A structured lookup (`[built]`, `agent/diagnose/taxonomy.py`); Path B schema + objection logic (`[built]`, `extract.py`/`objection.py`); the actual model call (`[pending]`) | partial |
| `DECIDE` | EV computation, `p_base` x `lift_prior` | `[pending]` — no `agent/decide/ev.py` yet; needs the Kaggle/IBM datasets fitted for `p_base` |
| `BOUNDS` | `check_bounds()`, the Law 3 gate | `[built]` — `agent/bounds/engine.py`, full rule register, differential test |
| `ACT` | Execute an accepted action against a `Rail` | `[built]` — `agent/act/executor.py`: `check_bounds()` first (Law 3), then a claim-then-act idempotency gate (§9.4) before ever calling the rail, dispatching `create_payment_link`, `reissue_artifact`, `create_mandate`, `retry_charge`, `initiate_refund`, `revoke_mandate`, `repair_mandate`, and message-only actions |
| `LISTEN` | Turn rail webhooks into `SYSTEM`-provenance facts | `[built]` — `agent/ingest/listen.py::facts_from_webhook()`, covering every event type `SimulatedRail` emits (payment captured/failed, mandate activated/revoked/notified, refund processed, link/invoice paid) |
| `SETTLE` | Attribute a `captured` payment via `recovery_ledger` | `[built]` — `agent/ledger/recovery.py`, Law 7's `UNIQUE(payment_id)` |
| `AUDITOR` *(not one of the seven)* | Extractor drift, bounds integrity, chain integrity — read-only, out-of-band | partial — chain integrity is exactly `Ledger.verify_chain()` (`[built]`); extractor-drift sampling and the bounds-integrity re-check job (§11.7) are `[pending]`, and there's no scheduler running the Auditor periodically |

No stage calls another directly anywhere in the code that exists — the
pieces that exist communicate by one module calling another's pure
function (e.g. the CLI's `demo` command calling `select_instrument()` then
`check_bounds()` then `SimulatedRail`), which is the "ledger is the only
bus" principle in miniature. `agent/api/app.py` (`[built]`, `uv run
trucommit serve`) gives `INGEST`/`LISTEN` a real HTTP endpoint —
`verify_and_ingest()` then `facts_from_webhook()` on every `POST
/webhooks/{source}` — but there is still no scheduler and no orchestrating
process that runs `DIAGNOSE -> DECIDE -> BOUNDS -> ACT` end to end on a
timer; each stage's pieces are called explicitly (by the CLI, by a test)
rather than by a running pipeline.

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

**`[pending]`**: the actual model call that would *produce* an
`ExtractionResult` from raw debtor text. Nothing in this codebase calls an
LLM. `tests/agent/test_injection_resistance.py` exercises the schema and
the family/class -> action-set mapping against worst-case hand-constructed
`ExtractionResult` instances standing in for "whatever a compromised model
might output" — a structural test, not an empirical one; see
`docs/THREAT_MODEL.md` for exactly what that does and doesn't prove.

§8's deemed-acceptance worked example (model as veto only, never as
establisher of fact) is `[built]` independently of the extractor itself —
`agent/diagnose/objection.py`'s `compute_objection_marker` and
`compute_deemed_acceptance` take an `ExtractionResult` as input, so they're
fully testable today with hand-built extractions and become live the
moment a real extractor produces one.

## Three families and their action sets (§11.2)

| Family | Meaning | Actions unlocked | Implemented? |
|---|---|---|---|
| A — Instrument failure | Money exists, rail failed/will fail | `retry_charge`, `repair_mandate`, `create_payment_link` | Diagnosis: `[built]` (`agent/diagnose/taxonomy.py`). Actions: typed (`agent/act/actions.py`), not orchestrated |
| B — Administrative blocker | Money + willingness exist, paperwork blocks | `reissue_artifact`, `request_reconciliation` | Typed only, no repair-orchestration module yet |
| C — Liquidity/willingness | Money isn't there or won't be released | `create_mandate`, `create_payment_link`, `send_reminder` | `select_instrument()` `[built]`; nothing calls it from a live diagnosis yet |
| D — Dispute | Contested obligation | `escalate_human` only | State machine handles `DISPUTED_FROZEN` `[built]`; the schema + action-set mapping is `[built]` and proven to never unlock anything beyond `escalate_human` for any family-D class (`tests/agent/test_injection_resistance.py`); no live model produces a family-D diagnosis yet |

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

`[pending]`. `agent/decide/` exists as an empty package. When built:

```python
ev_paise = int(p_base * lift_prior * recoverable_paise) - cost_paise(action)
#          ^^^^^^ fitted, calibrated   ^^^^^^^^^^ a declared prior, typed Prior[float]
```

`p_base` needs the Kaggle IBM Late Payment Histories / Payment Date
Prediction datasets fitted and calibrated (Brier score + reliability
diagram, per §17.5) — not done. `lift_prior` needs to be typed as
`Prior[float]`, not `float`, specifically so a code reviewer can't mistake
it for a fitted value — that type doesn't exist yet either. The bounds
engine's `EV_FLOOR` rule (`agent/bounds/rules.yaml`) already checks
`decision.ev_paise > 0` structurally, ready for whatever produces a real
`Decision` once this exists.

## Typed actions and their Razorpay objects (§11.5)

`[built]` as metadata — `agent/act/actions.py`. See the generated
"Escalation ladder" table in `docs/BOUNDS.md` for the full action-by-action
breakdown, which is produced from this same module so it can't drift from it.

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

`[pending]` as running behaviour (there's no scheduler or live service to
degrade), but the *properties* that would make each failure mode safe are
already true of the pieces that exist: Family A diagnosis never touches a
model (`agent/diagnose/taxonomy.py` has no model dependency at all, so
"LLM unavailable" degrades to exactly this path with zero code change
needed); `check_bounds()` fails closed (an exception during evaluation
propagates rather than defaulting to PASS); the ledger's chain-integrity
check refuses to silently continue past a broken hash (`verify_chain()`
raises, naming the exact `seq`).
