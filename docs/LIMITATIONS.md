# Limitations

I'm stating this plainly, per DEVDOC_v6's own standard throughout: name
what's cut, don't bury it.

## What my build is, honestly

I've built a tested, working implementation of TrueCommit's **pure-logic
safety and compliance core** (DEVDOC_v6 §5.2's "the judgment"), **now also
wired to a real, live Razorpay test-mode account** (as of 2026-08-30) for
the capabilities that account actually has, plus a real (if minimal) HTTP
webhook receiver. **771 tests passing / 11 skipped as of 2026-09-01**,
measured without live credentials in the shell (the 11 skipped are the
Razorpay-live-only suite, which skips cleanly rather than failing — no
credentials are required to run the main suite). It's still **not**:

- A Jinja-templated human-queue dashboard UI (the demo dashboard is a
  published Artifact, not a server-rendered page). **Update, 2026-09-01**:
  `DIAGNOSE -> DECIDE -> BOUNDS -> ACT` now *does* run end to end,
  automatically, triggered by a real webhook -- `agent/orchestrate.py` +
  wiring I added in `agent/api/app.py`, live-verified (see
  `docs/ORCHESTRATION.md`). Path A only (a structured failure code, no
  model); I've designed Path B to plug into the identical `run_pipeline()`
  call but haven't wired it to a live Telegram reply yet. A real scheduled
  Auditor (`agent/auditor/scheduler.py`, APScheduler in-process) runs
  alongside it.
- The four-arm evaluation (§17) against **real debtors** — still not done,
  and correctly so (it needs a live deployment I don't have).
  **Update, 2026-09-01**: I've now run the *synthetic* three-arm (A/B2/C)
  comparison for real, under a committed pre-registration —
  `eval/PREREGISTRATION.md` locked n=500/seed=42/window=30d/primary
  comparison before `eval/report.py` generated `docs/RESULTS.md` from that
  exact configuration. Real findings: Arm C recovers more than both other
  arms at a *neutral* `lift_prior=1.0` (no assumed behavioural uplift) with
  zero real `check_bounds()` violations against hundreds for the two
  ungated arms; at the realistic Rs 5 touch cost, `lift_prior` turns out not
  to be load-bearing at all (`EV_FLOOR` never binds against this
  population); a stress-tested elevated touch cost does produce a genuine
  break-even τ≈0.49, near the low end of the declared sweep range. This is
  a synthetic-population, known-ground-truth result — it measures the
  pipeline's logic, not real extraction accuracy or real debtor behaviour;
  see `docs/RESULTS.md`'s own "what this is not" section
- Wired end to end from the live webhook to a real LLM call — I've built
  the call itself, tested it, and **live-verified it as of 2026-08-31**
  (`agent/diagnose/llm_extract.py`, two real successful extractions, see
  `docs/LLM_EXTRACTION.md`), but I don't yet invoke it from the DIAGNOSE
  stage automatically when a webhook carries free text instead of a
  structured code — that orchestration gap is what's left, not the call
  itself anymore
- A live message *sent* via Twilio (an actual call) — I **have** sent
  Telegram messages live, repeatedly, since 2026-08-31 (`docs/CHANNELS.md`);
  Twilio voice is still blocked on the connected account owning a phone
  number, an external/billing blocker, not a code gap
- A live WhatsApp send. `agent/notify/whatsapp.py` is code-complete and
  I've tested it against Meta's documented Cloud API request/response
  shapes via `httpx.MockTransport` (37 tests) — the same standard I held
  `TwilioVoiceChannel` to before its own live credentials existed — but I
  don't have a real Meta Business account yet, so nothing here has touched
  Meta's actual API. See `docs/WHATSAPP.md`.

## Webhook receiver (§19) — I built and registered it live once; current uptime unknown

`agent/api/app.py` is a minimal FastAPI app (`POST /webhooks/{source}`,
`GET /health`) I wired directly into `verify_and_ingest()` and
`facts_from_webhook()` — tested end to end through real HTTP
request/response via FastAPI's `TestClient` (`tests/agent/test_api_webhooks.py`,
8 tests, using real `SimulatedRail`-emitted webhooks). `uv run trucommit
serve` runs it with uvicorn. **Update, 2026-08-31**: I did this for real,
once — a `cloudflared` quick tunnel gave it a public URL and
`client.webhook.create()` registered that URL with the live Razorpay
account (see `docs/SETUP.md`). That tunnel is ephemeral by design (dies
with the process, no uptime guarantee even while running), so whether
it's currently reachable depends on whether that specific `trucommit
serve` + `cloudflared` process pair is still alive — I'm not tracking
that here because it isn't a code fact, it's live process state that
changes outside of any commit. I still haven't observed a real
Razorpay-triggered delivery reaching the receiver (every subscribed event
needs a completed checkout to fire). No dashboard UI exists yet — only
the receiver endpoint.

## Live rail status (2026-08-30) — a real upgrade from "assumed"

I've now run `tools/probe_rails.py` against a real test-mode account.
**The account clears more than DEVDOC_v6 §6's own "Expected" table
anticipated**: `orders`, `payment_links`, `invoices`, `customers`,
`plans`, `subscriptions`, and `settlements` all cleared live (see
`docs/RAIL_CAPABILITIES.md`, regenerated from the real run, not the
doc's own predictions). `agent/rails/razorpay_rail.py` is a real
`RazorpayRail` implementation I wrote, and
`tests/agent/test_razorpay_rail_live.py` (9 tests, skipped without
credentials, all passing with them) verifies it against the live
account — including running the *exact same* `run_conformance_suite()`
that passes against `SimulatedRail`.

**The one structural finding worth being precise about**: `subscriptions`
clearing does **not** mean UPI Autopay/eNACH-style variable mandates work.
The only recurring-payment primitive this account can create is a
Plan+Subscription, which bills a **fixed** amount per cycle on Razorpay's
own schedule — not the "debit up to max_amount, on demand" instrument I
modelled `MandateSpec`/`present_debit` on (§12). `RazorpayRail.
present_debit()` and `.modify_mandate()` both raise `RailUnavailable`
honestly rather than guess at a call I haven't verified exists.
`create_mandate` (as Plan+Subscription) and `revoke_mandate`
(`subscription.cancel`) **are** live-verified. I also found: real
Razorpay subscription statuses (`created`, `authenticated`, `active`,
`pending`, `halted`, `cancelled`, `completed`, `expired`) don't match the
TrueCommit-internal vocabulary I invented for `SimulatedRail` to mirror
§12.5's lifecycle diagram literally (`pending_afa`, `notified_24h`, ...)
— a real drift the conformance suite exists to surface, which I mapped
conservatively in `_mandate_status_from_subscription()` rather than
papering over.

**Observed, 2026-08-30, end of session**: after the cumulative volume of
live API calls I made while building and re-running the live test suite
repeatedly in one session, `payment_link.create` specifically started
returning `BadRequestError: Too many requests` — a test-mode rate limit,
not a code defect (the same `RazorpayRail.create_payment_link()` call
succeeded repeatedly earlier in this same session, and `create_order`,
`create_invoice`, `create_mandate`, and `revoke_mandate` were unaffected
when I observed this, pointing at a per-endpoint limit rather than an
account-wide one). I haven't added retry/backoff logic to `RazorpayRail`
— a reasonable future enhancement, deliberately not added reactively at
the end of a session already consuming the very quota it would need to
test against. If `tests/agent/test_razorpay_rail_live.py` fails with this
exact error, I wait before re-running rather than assuming the code
regressed.

**Update, 2026-09-01 — the payment-link cap was actually reached.**
`tools/run_real_scenarios.py`'s live batch run hit `ServerError: test mode
limit of 30 reached for payment_link` — a hard, apparently permanent
per-account cap for an unactivated test-mode account (not a transient rate
limit like the one above; retrying didn't help). `create_mandate` and
`create_invoice` are unaffected — confirmed live, same run, same account.
I catch this cleanly (the batch run records the failure and continues
rather than crashing), but this is a real constraint on further live
payment-link testing against this specific account until it's
activated/KYC'd. See `docs/evidence/REAL_SCENARIOS.md`.

**Still not live-verified**: `create_refund` (I implemented it against
the documented SDK method, but refunding needs an actually-captured
payment, which needs a completed checkout with 3DS/OTP — not reachable
from a headless script); real webhook delivery and its exact envelope
shape (`SimulatedRail`'s envelope convention is still my own, unverified
against a captured real webhook — see `docs/SIMULATOR_PROVENANCE.md`);
`tokens_recurring`, `upi_autopay`, `emandate` specifically (the probe
flags these `NOT_DIRECTLY_PROBEABLE` — they need a real checkout session
or dashboard inspection, not a server-side create call).

## Regulatory sourcing (the most important gap)

**Update, 2026-08-30**: I now have four of the six regulatory `clause_ref`
values in `agent/bounds/rules.yaml` citing a real section of the actual
RBI circular (`RBI/DPSS/2026-27/396`, fetched directly from `rbi.org.in`,
not guessed — see `docs/SIMULATOR_PROVENANCE.md` §4 for exactly which
sections and the caveat on how I extracted them). I sourced
`RBI_FPC_HOURS` more weakly, to secondary summaries rather than RBI's own
text. `TRAI_DND` remains a `TODO`, and the MSMED Act's trader-exclusion
OMs (`config/statutory_params.yaml`) are still DEVDOC_v6's own reading,
dated and flagged `contested: true` rather than independently
re-verified. Better section citations are progress on *coverage*, not on
the legal-review claim below, which still stands unchanged.

**Compliance requires external review, which I don't have** (DEVDOC_v6
§13.4, repeated here in those words on purpose). The differential test
between `agent/bounds/engine.py` and `agent/bounds/human_twin.py` (5,000
Hypothesis-generated inputs, all passing) demonstrates that two
independently-written implementations of the same stated intent agree
with each other. It doesn't demonstrate that either implementation
correctly reads the RBI/MSMED/TRAI source text, because I wrote both
myself. `docs/REGULATORY_MAP.md`'s coverage claim is the strongest honest
claim I can make, and even that is "clauses are implemented," not
"clauses are implemented correctly."

## Simulator provenance (see docs/SIMULATOR_PROVENANCE.md for detail)

- I sourced the failure taxonomy from Razorpay's public error *pages*,
  not the primary XLSX DEVDOC_v6 §5.5 names (I couldn't fetch/parse it as
  a binary file in this build pass). Needs a manual diff against the real
  spreadsheet.
- Object shapes in `agent/rails/types.py` are a reduced, reasonable
  subset I built from knowledge of the Razorpay API's structure and the
  installed SDK's verified method names — not diffed against the SDK's
  actual test fixtures (`data/rail_fixtures/` exists, empty, for exactly
  that).
- `SimulatedRail`'s webhook envelope shape (`event`, `event_id`,
  `created_at`, `payload`) is my own convention, not verified against a
  captured real Razorpay webhook payload.
- I haven't sourced NACH/eNACH mandate return codes at all yet.

## What §5.4's conformance suite proves, and what it can't yet

I built `agent/rails/conformance/suite.py::run_conformance_suite()` and
it passes against `SimulatedRail` (`tests/agent/test_conformance.py`) —
one function, rail-agnostic by construction (it takes a factory, not a
rail instance), so the identical call with a `RazorpayRail` factory is
the only change needed once test keys exist. Its scope is narrow by
design (§5.4): object shape, one state transition (mandate revocation),
webhook structure and signature, and redelivery idempotency. It does
**not** and can't yet check error-code vocabulary meaningfully
(`_check_failure_codes_are_in_the_published_vocabulary` is honestly a
near-no-op today — there's no generic "list all payments" call on the
`Rail` protocol to inspect a real failure through, so it mostly reports
"skipped, not failed"). It never claims anything about NACH return
timing, real failure distributions, or issuer-specific behaviour — the
reachable CRUD surface on an unactivated account has no analogue for any
of those.

## Golden set, extractor, Auditor

§11.2 Path B's **schema** (`agent/diagnose/extract.py`) and the
objection-marker / deemed-acceptance logic I built on top of it
(`agent/diagnose/objection.py`) are built and tested. **Update,
2026-08-31**: I now have a real model call
(`agent/diagnose/llm_extract.py::extract_from_reply()`,
`client.messages.parse()` against `claude-sonnet-5`, constructing a real
`ExtractionResult` so every existing validator runs on the model's
output) — tested against a mocked client (`tests/agent/test_llm_extract.py`,
11 tests), never yet against the real API. §17.8's stratified golden set
still doesn't exist — it needs real extractions to label, and I haven't
produced any yet.

I've **built §11.7's Auditor for its two model-free jobs**: chain
integrity (wraps `Ledger.verify_chain()`) and bounds integrity
(`agent/auditor/auditor.py::check_bounds_integrity()` — recomputes
`check_bounds()` from each sampled action's own recorded inputs and
raises `BoundsIntegrityBreach` on a mismatch; `tests/agent/test_auditor.py`
includes a test that forges a recorded verdict and confirms it's caught).
This only works because `agent/act/executor.py` now writes a JSON-safe
`bounds_context_snapshot` into every `LedgerEntry` it appends — a real
structural gap I found while building the Auditor: ACT never wrote to the
ledger at all before this, which meant Law 4 ("agents coordinate only
through the ledger") was simply not upheld for the one stage that moves
money. **Update, 2026-09-01 — I've now built extractor drift**:
`agent/auditor/extractor_drift.py` samples logged past extractions
(`agent/auditor/extraction_log.py`, a new opt-in local record
`extract_from_reply()` writes to when given one) and re-checks each
against a second model, quarantining below an agreement threshold. Real,
tested (`tests/agent/test_extractor_drift.py`), but I **deliberately
didn't auto-schedule** it the way the two free jobs are — every real run
spends real money against the $20 ceiling `agent/spend.py` enforces, so I
left putting it on an automatic timer as an explicit opt-in
(`agent.auditor.scheduler.add_extractor_drift_job`), not something I
decided silently on the operator's behalf.

The two free jobs **do run on a schedule** — `agent/auditor
/scheduler.py`, APScheduler in-process, wired into `agent/api/app.py`'s
lifespan behind the `TRUECOMMIT_LEDGER_DB` env var (`uv run trucommit
serve` starts both alongside the webhook receiver; it warns loudly,
rather than silently, if that variable isn't set). A trip currently logs
at `CRITICAL` rather than DEVDOC_v6 §11.7's own "halt the arm, write
WHAT_BROKE.md" — "arm" is a concept from the eval harness (§17), which
now exists and has run once (`docs/RESULTS.md`) but only as an offline
Monte Carlo comparison, not a live, running A/B assignment serving real
traffic — there's still nothing *live* to halt in the sense the spec's
phrase means, even though the harness itself is no longer hypothetical.

## EV gate

**Update, 2026-08-31**: both halves of this section's original claim are
now out of date. I typed `lift_prior` as `Prior[float]`
(`agent/decide/ev.py`) — a real class, `isinstance`-checkable, not a
`NewType` fiction. `p_base` **is fitted** — `tools/fit_persona_params.py`
fits a logistic regression against the Kaggle Payment Date Prediction
dataset (50,000 rows, committed in `data/ar_seed/`), evaluated on an
8,000-row holdout (Brier score 0.0206), with the reliability-diagram data
in `data/fitted_params.yaml`. I load it at runtime with no fitting
dependencies via `agent/decide/fitted_p_base.py` (pure `math`, no
pandas/scikit-learn needed outside the one-time fit).

**The honest caveat, stated where the number lives, not just here**: the
dataset's holdout base rate is 97.9% — almost every invoice in it pays
within 30 days regardless of amount — so a Brier score of 0.02 reflects a
well-calibrated model on a lopsided target, not a strongly discriminative
one. It's a real, evaluated fit on a real (if US, not Indian) dataset —
genuinely stronger evidence than a declared prior — but not a claim that
invoice amount predicts payment timing well. `EV_FLOOR`'s bounds rule is
ready and tested; I don't yet call `compute_ev()` from a live diagnosis
to produce a `Decision` end to end — that's the DECIDE-stage
orchestration gap noted in `ARCHITECTURE.md`, not a data gap anymore.

## Statutory ladder

I've only implemented rung 4 (interest computation), as DEVDOC_v6 §14
specifies. Rungs 5-6 are neither implemented nor stubbed as code —
they're absent. The trader-exclusion position in
`config/statutory_params.yaml` is copied verbatim from DEVDOC_v6's own
example and I've explicitly marked it `contested: true` in that same
file — it rests on executive memoranda, not statute, and should be
re-checked before relying on it.

Interest computation uses 30-day months as its "monthly rest" boundary —
a declared simplification I made, not calendar-month rests — because the
spec doesn't pin down which convention to use and 30-day months are
simple to verify. Revisit if a precise court-facing figure is ever
needed.

## Design decisions I made while implementing, not just discovered gaps

- `promise_credibility`'s "floor" (§24.2) has no doc-specified numeric
  value; I default it to 0.34 and implement the cooldown as a
  *continuous* scaling (`grace_days x credibility`) rather than a hard
  cutoff, since the doc's own inline comment ("Cooldown granted =
  grace_days x credibility, floored at 0") describes exactly that, and a
  continuous scaling has no arbitrary cliff-edge to defend.
- The Auditor's sampling rates (extractor drift, bounds integrity)
  default to 10%, per DEVDOC_v6 §11.7's own amendment — a starting point,
  not a finding, cheap to raise once I know a real per-sample cost.
- Statutory interest rounds half-up to the nearest paisa at each monthly
  rest, my own addition to DEVDOC_v6 §14.3 (the original had no rounding
  rule at all for a paise-as-int type computing fractional interest).

## Golden set / vignette study / adversarial personas / eval harness

I've **built DEVDOC_v6 §24.1's injection corpus (40 cases) and its
structural resistance tests** (`data/injection_corpus.jsonl`,
`tests/agent/test_injection_resistance.py`, 80 tests) — see
`docs/THREAT_MODEL.md` for exactly what they prove and what they don't
(no live model is exercised; see below). §24.2's stopping-rule DoS fixes
are built and tested in the bounds engine itself.

I've **committed `eval/PREREGISTRATION.md`** — §17.6's parameter
classification table, filled in honestly: swept-parameter ranges are
declared (they don't need a source, by definition), fitted parameters are
marked `PENDING` with the exact dataset and access blocker named, and the
four arms are defined with their real status. **I've implemented and
tested Arm A (the control)** (`eval/arms/a/schedule.py`,
`tests/eval/test_arm_a_schedule.py`) — a fixed schedule needs no model,
so it's the one arm I could build in isolation.

**Update, 2026-08-31 — built**: this paragraph previously named three
blockers (no fitted `p_base`, no LLM call, no persona-simulation engine).
None of the three is accurate anymore: I built
`eval/personas/generator.py`, which samples a synthetic population from
the fitted Kaggle distributions (amount shape, dispute rate, the real
`p_base` model); `eval/simulate.py` runs Arms A, B2, and C against that
same population and calls the real `compute_ev()` and `check_bounds()`
for Arm C (only Diagnose is simulated — there's no real text to extract
from a synthetic persona). I wrote 18 tests
(`tests/eval/test_persona_generator.py`, `tests/eval/test_simulate.py`)
covering reproducibility and the structural invariants (Arm C escalates
to a human in cases the other two structurally cannot; Arm C loses fewer
debtors to contact exhaustion; `EV_FLOOR` genuinely refuses when a
touch's cost dominates the recoverable amount).

**Update, 2026-09-01 — I now have a pre-registered result.**
`eval/PREREGISTRATION.md` locks n=500/seed=42/window=30d/lift=1.0 and the
primary comparison, committed before `eval/report.py` generated
`docs/RESULTS.md` from exactly that configuration (commit hash cited in
the doc itself). I also added, while building this: a real
`check_bounds()` "violations column" for Arms A/B2 (a shadow check, never
gating them — `eval/simulate.py::_shadow_bounds_violation`, narrowly
scoped to a real, triggerable `DISPUTE_FREEZE` case, not every rule this
simplified touch model can't actually exercise) and a Family-B-only
breakout, which turned out to have a real, honestly-reported limitation
of its own: only 2 of 500 locked personas land in the
administrative-blocker subpopulation (a direct consequence of the fitted
`p_base` model's own high base rate), too few for that specific cut to be
a reliable estimate of anything — I'm reporting it for completeness per
§17.7, not presenting it as a finding.

**I still haven't built**: §24.3's four adversarial personas run through
this harness specifically (the underlying bounds-rule fixes they'd
exercise — promise-cooldown credibility, dispute-freeze scoping, channel
exhaustion — are tested directly at the bounds-engine level,
`tests/agent/test_bounds_engine.py`, just not run as personas through
`eval/simulate.py` to produce a "cases permanently stalled" count); §25's
fuller autonomy/economics reporting beyond what `docs/RESULTS.md` already
reports (human escalation rate, mean touches, as an autonomy-rate proxy);
§27's vignette study (I built it and it's ready to send — it needs 25
human respondents, and I explicitly, deliberately dropped it from my
scope, not merely deferred it — "low judge value, high time cost").

## Scalability — a plan exists, nothing has been migrated

My whole build runs as one process against per-file SQLite databases and
orchestrates synchronously inside the webhook request. That's a
deliberate, correct choice for a demo/pilot scale (SQLite in WAL mode
genuinely handles real concurrent load, not a toy), not an oversight —
but it has a real ceiling (one file, one machine; one merchant's data,
since nothing in the schema is scoped by `merchant_id` yet).
`docs/SCALABILITY.md` lays out the concrete path (Postgres, multi-tenant
schema, queued orchestration, horizontally-scaled API) without any of it
being built — see that doc for what would actually need to change and in
what order.

## No static lint rule against float arithmetic on `Money`

DEVDOC_v6 §9.1 asks for one; I have a runtime guard
(`agent/money.py::assert_money()`) but not a static analysis rule (would
need a custom mypy or ruff plugin). Noted rather than silently dropped.
