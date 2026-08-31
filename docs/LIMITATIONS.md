# Limitations

Stated plainly, per DEVDOC_v6's own standard throughout: name what's cut,
don't bury it.

## What this build is, honestly

A tested, working implementation of TrueCommit's **pure-logic safety and
compliance core** (DEVDOC_v6 §5.2's "the judgment"), **now also wired to a
real, live Razorpay test-mode account** (as of 2026-08-30) for the
capabilities that account actually has, plus a real (if minimal) HTTP
webhook receiver. **606 tests passing / 11 skipped as of 2026-08-31**,
measured without live credentials in the shell (the 11 skipped are the
Razorpay-live-only suite, which skips cleanly rather than failing —
no credentials are required to run the main suite). It is still **not**:

- A running multi-stage pipeline with a dashboard UI — no Jinja templates,
  no human-queue view, and no scheduler running `DIAGNOSE -> DECIDE ->
  BOUNDS -> ACT` end to end. There *is* now a real webhook receiver
  (`agent/api/app.py`, `uv run trucommit serve`) and a real scheduled
  Auditor (`agent/auditor/scheduler.py`, APScheduler in-process) running
  alongside it — see below for exactly what each does and doesn't close.
- The four-arm evaluation (§17), run for real — the harness itself now
  exists (`eval/simulate.py`, `eval/personas/generator.py`, see the "Golden
  set, extractor, Auditor" section below), but no arm has been run under a
  committed pre-registration yet, and won't be until `eval/PREREGISTRATION.md`
  locks in population size, window length, and the primary comparison first
- Wired end to end from the live webhook to a real LLM call — the call
  itself now exists, is tested, and **is live-verified as of 2026-08-31**
  (`agent/diagnose/llm_extract.py`, two real successful extractions, see
  `docs/LLM_EXTRACTION.md`), but nothing yet invokes it from the DIAGNOSE
  stage automatically when a webhook carries free text instead of a
  structured code — that orchestration gap is what's left, not the call
  itself anymore
- A live message *sent* (as opposed to credentials confirmed) — Telegram
  and Twilio-voice both have real, live-verified credentials as of
  2026-08-31 (`docs/CHANNELS.md`), but `send()` itself (an actual message,
  an actual call) hasn't been exercised live: Telegram has no `chat_id` yet
  (nobody has messaged the bot), and Twilio needs a real destination number
  that wasn't provided

## Webhook receiver (§19) — built and registered live once; current uptime unknown

`agent/api/app.py` is a minimal FastAPI app (`POST /webhooks/{source}`,
`GET /health`) wired directly into `verify_and_ingest()` and
`facts_from_webhook()` — tested end to end through real HTTP request/response
via FastAPI's `TestClient` (`tests/agent/test_api_webhooks.py`, 8 tests,
using real `SimulatedRail`-emitted webhooks). `uv run trucommit serve` runs
it with uvicorn. **Update, 2026-08-31**: this has been done for real, once —
a `cloudflared` quick tunnel gave it a public URL and `client.webhook.create()`
registered that URL with the live Razorpay account (see `docs/SETUP.md`).
That tunnel is ephemeral by design (dies with the process, no uptime
guarantee even while running), so whether it's currently reachable depends
on whether that specific `trucommit serve` + `cloudflared` process pair is
still alive — not tracked here because it isn't a code fact, it's live
process state that changes outside of any commit. A real Razorpay-triggered
delivery reaching the receiver has still not been observed (every
subscribed event needs a completed checkout to fire). No dashboard UI
exists yet — only the receiver endpoint.

## Live rail status (2026-08-30) — a real upgrade from "assumed"

`tools/probe_rails.py` has now run against a real test-mode account.
**The account clears more than DEVDOC_v6 §6's own "Expected" table
anticipated**: `orders`, `payment_links`, `invoices`, `customers`,
`plans`, `subscriptions`, and `settlements` all cleared live (see
`docs/RAIL_CAPABILITIES.md`, regenerated from the real run, not the
doc's own predictions). `agent/rails/razorpay_rail.py` is a real
`RazorpayRail` implementation, and `tests/agent/test_razorpay_rail_live.py`
(9 tests, skipped without credentials, all passing with them) verifies it
against the live account — including running the *exact same*
`run_conformance_suite()` that passes against `SimulatedRail`.

**The one structural finding worth being precise about**: `subscriptions`
clearing does **not** mean UPI Autopay/eNACH-style variable mandates work.
The only recurring-payment primitive this account can create is a
Plan+Subscription, which bills a **fixed** amount per cycle on Razorpay's
own schedule — not the "debit up to max_amount, on demand" instrument
`MandateSpec`/`present_debit` were modelled on (§12). `RazorpayRail.
present_debit()` and `.modify_mandate()` both raise `RailUnavailable`
honestly rather than guess at a call this build hasn't verified exists.
`create_mandate` (as Plan+Subscription) and `revoke_mandate`
(`subscription.cancel`) **are** live-verified. Also found: real Razorpay
subscription statuses (`created`, `authenticated`, `active`, `pending`,
`halted`, `cancelled`, `completed`, `expired`) don't match the
TrueCommit-internal vocabulary `SimulatedRail` invented to mirror §12.5's
lifecycle diagram literally (`pending_afa`, `notified_24h`, ...) — a real
drift the conformance suite exists to surface, mapped conservatively in
`_mandate_status_from_subscription()` rather than papered over.

**Observed, 2026-08-30, end of session**: after the cumulative volume of
live API calls made while building and re-running the live test suite
repeatedly in one session, `payment_link.create` specifically started
returning `BadRequestError: Too many requests` — a test-mode rate limit,
not a code defect (the same `RazorpayRail.create_payment_link()` call
succeeded repeatedly earlier in this same session, and `create_order`,
`create_invoice`, `create_mandate`, and `revoke_mandate` were unaffected
when this was observed, pointing at a per-endpoint limit rather than an
account-wide one). No retry/backoff logic exists in `RazorpayRail` — a
reasonable future enhancement, deliberately not added reactively at the
end of a session already consuming the very quota it would need to test
against. If `tests/agent/test_razorpay_rail_live.py` fails with this exact
error, wait before re-running rather than assuming the code regressed.

**Still not live-verified**: `create_refund` (implemented against the
documented SDK method, but refunding needs an actually-captured payment,
which needs a completed checkout with 3DS/OTP — not reachable from a
headless script); real webhook delivery and its exact envelope shape
(`SimulatedRail`'s envelope convention is still this build's own, unverified
against a captured real webhook — see `docs/SIMULATOR_PROVENANCE.md`);
`tokens_recurring`, `upi_autopay`, `emandate` specifically (the probe
flags these `NOT_DIRECTLY_PROBEABLE` — they need a real checkout session or
dashboard inspection, not a server-side create call).

## Regulatory sourcing (the most important gap)

**Update, 2026-08-30**: four of the six regulatory `clause_ref` values in
`agent/bounds/rules.yaml` now cite a real section of the actual RBI
circular (`RBI/DPSS/2026-27/396`, fetched directly from `rbi.org.in`, not
guessed — see `docs/SIMULATOR_PROVENANCE.md` §4 for exactly which sections
and the caveat on how they were extracted). `RBI_FPC_HOURS` is sourced more
weakly, to secondary summaries rather than RBI's own text. `TRAI_DND`
remains a `TODO`, and the MSMED Act's trader-exclusion OMs
(`config/statutory_params.yaml`) are still DEVDOC_v6's own reading, dated
and flagged `contested: true` rather than independently re-verified. Better
section citations are progress on *coverage*, not on the legal-review claim
below, which still stands unchanged.

**Compliance requires external review, which this project does not have**
(DEVDOC_v6 §13.4, repeated here in those words on purpose). The
differential test between `agent/bounds/engine.py` and
`agent/bounds/human_twin.py` (5,000 Hypothesis-generated inputs, all
passing) demonstrates that two independently-written implementations of
the same stated intent agree with each other. It does not demonstrate that
either implementation correctly reads the RBI/MSMED/TRAI source text,
because the same person wrote both. `docs/REGULATORY_MAP.md`'s coverage
claim is the strongest honest claim available, and even that is "clauses
are implemented," not "clauses are implemented correctly."

## Simulator provenance (see docs/SIMULATOR_PROVENANCE.md for detail)

- The failure taxonomy was sourced from Razorpay's public error *pages*,
  not the primary XLSX DEVDOC_v6 §5.5 names (couldn't be fetched/parsed as
  a binary file in this build pass). Needs a manual diff against the real
  spreadsheet.
- Object shapes in `agent/rails/types.py` are a reduced, reasonable subset
  built from knowledge of the Razorpay API's structure and the installed
  SDK's verified method names — not diffed against the SDK's actual test
  fixtures (`data/rail_fixtures/` exists, empty, for exactly that).
- `SimulatedRail`'s webhook envelope shape (`event`, `event_id`,
  `created_at`, `payload`) is this build's own convention, not verified
  against a captured real Razorpay webhook payload.
- NACH/eNACH mandate return codes are not sourced at all yet.

## What §5.4's conformance suite proves, and what it can't yet

`agent/rails/conformance/suite.py::run_conformance_suite()` is built and
passing against `SimulatedRail` (`tests/agent/test_conformance.py`) — one
function, rail-agnostic by construction (it takes a factory, not a rail
instance), so the identical call with a `RazorpayRail` factory is the only
change needed once test keys exist. Its scope is narrow by design (§5.4):
object shape, one state transition (mandate revocation), webhook structure
and signature, and redelivery idempotency. It does **not** and can't yet
check error-code vocabulary meaningfully (`_check_failure_codes_are_in_
the_published_vocabulary` is honestly a near-no-op today — there's no
generic "list all payments" call on the `Rail` protocol to inspect a real
failure through, so it mostly reports "skipped, not failed"). Never claims
anything about NACH return timing, real failure distributions, or
issuer-specific behaviour — the reachable CRUD surface on an unactivated
account has no analogue for any of those.

## Golden set, extractor, Auditor

§11.2 Path B's **schema** (`agent/diagnose/extract.py`) and the objection-marker
/ deemed-acceptance logic built on top of it (`agent/diagnose/objection.py`)
are built and tested. **Update, 2026-08-31**: a real model call now exists
(`agent/diagnose/llm_extract.py::extract_from_reply()`, `client.messages.parse()`
against `claude-sonnet-5`, constructing a real `ExtractionResult` so every
existing validator runs on the model's output) — tested against a mocked
client (`tests/agent/test_llm_extract.py`, 11 tests), never yet against the
real API. §17.8's stratified golden set still does not exist — it needs
real extractions to label, and none have been produced yet.

§11.7's Auditor is **built for its two model-free jobs**: chain integrity
(wraps `Ledger.verify_chain()`) and bounds integrity (`agent/auditor
/auditor.py::check_bounds_integrity()` — recomputes `check_bounds()` from
each sampled action's own recorded inputs and raises `BoundsIntegrityBreach`
on a mismatch; `tests/agent/test_auditor.py` includes a test that forges a
recorded verdict and confirms it's caught). This only works because
`agent/act/executor.py` now writes a JSON-safe `bounds_context_snapshot`
into every `LedgerEntry` it appends — a real structural gap found while
building the Auditor: ACT never wrote to the ledger at all before this,
which meant Law 4 ("agents coordinate only through the ledger") was simply
not upheld for the one stage that moves money. **Extractor drift is not
built** — it needs a live model producing real extractions to sample and
re-run.

Both model-free jobs **do now run on a schedule** — `agent/auditor
/scheduler.py`, APScheduler in-process, wired into `agent/api/app.py`'s
lifespan behind the `TRUECOMMIT_LEDGER_DB` env var (`uv run trucommit
serve` starts both alongside the webhook receiver; it warns loudly, rather
than silently, if that variable isn't set). A trip currently logs at
`CRITICAL` rather than DEVDOC_v6 §11.7's own "halt the arm, write
WHAT_BROKE.md" — "arm" is a concept from the eval harness (§17), which
doesn't exist yet, so there's nothing to halt in the sense the spec means.

## EV gate

**Update, 2026-08-31**: both halves of this section's original claim are
now out of date. `lift_prior` is typed as `Prior[float]`
(`agent/decide/ev.py`) — a real class, `isinstance`-checkable, not a
`NewType` fiction. `p_base` **is fitted** —
`tools/fit_persona_params.py` fits a logistic regression against the
Kaggle Payment Date Prediction dataset (50,000 rows, committed in
`data/ar_seed/`), evaluated on an 8,000-row holdout (Brier score 0.0206),
with the reliability-diagram data in `data/fitted_params.yaml`. Loaded at
runtime with no fitting dependencies via `agent/decide/fitted_p_base.py`
(pure `math`, no pandas/scikit-learn needed outside the one-time fit).

**The honest caveat, stated where the number lives, not just here**: the
dataset's holdout base rate is 97.9% — almost every invoice in it pays
within 30 days regardless of amount — so a Brier score of 0.02 reflects a
well-calibrated model on a lopsided target, not a strongly discriminative
one. It's a real, evaluated fit on a real (if US, not Indian) dataset —
genuinely stronger evidence than a declared prior — but not a claim that
invoice amount predicts payment timing well. `EV_FLOOR`'s bounds rule is
ready and tested; nothing yet calls `compute_ev()` from a live diagnosis to
produce a `Decision` end to end — that's the DECIDE-stage orchestration
gap noted in `ARCHITECTURE.md`, not a data gap anymore.

## Statutory ladder

Only rung 4 (interest computation) is implemented, as DEVDOC_v6 §14
specifies. Rungs 5-6 are neither implemented nor stubbed as code — they're
absent. The trader-exclusion position in `config/statutory_params.yaml` is
copied verbatim from DEVDOC_v6's own example and is explicitly marked
`contested: true` in that same file — it rests on executive memoranda, not
statute, and should be re-checked before relying on it.

Interest computation uses 30-day months as its "monthly rest" boundary — a
declared simplification, not calendar-month rests — because the spec
doesn't pin down which convention to use and 30-day months are simple to
verify. Revisit if a precise court-facing figure is ever needed.

## Design decisions made while implementing, not just discovered gaps

- `promise_credibility`'s "floor" (§24.2) has no doc-specified numeric
  value; this build defaults it to 0.34 and implements the cooldown as a
  *continuous* scaling (`grace_days x credibility`) rather than a hard
  cutoff, since the doc's own inline comment ("Cooldown granted = grace_days
  x credibility, floored at 0") describes exactly that, and a continuous
  scaling has no arbitrary cliff-edge to defend.
- The Auditor's sampling rates (extractor drift, bounds integrity) default
  to 10%, per DEVDOC_v6 §11.7's own amendment — a starting point, not a
  finding, cheap to raise once a real per-sample cost is known.
- Statutory interest rounds half-up to the nearest paisa at each monthly
  rest, per this build's own addition to DEVDOC_v6 §14.3 (the original had
  no rounding rule at all for a paise-as-int type computing fractional
  interest).

## Golden set / vignette study / adversarial personas / eval harness

DEVDOC_v6 §24.1's **injection corpus (40 cases) and its structural
resistance tests are built** (`data/injection_corpus.jsonl`,
`tests/agent/test_injection_resistance.py`, 80 tests) — see
`docs/THREAT_MODEL.md` for exactly what they prove and what they don't
(no live model is exercised; see below). §24.2's stopping-rule DoS fixes
are built and tested in the bounds engine itself.

**`eval/PREREGISTRATION.md` is committed** — §17.6's parameter classification
table, filled in honestly: swept-parameter ranges are declared (they don't
need a source, by definition), fitted parameters are marked `PENDING` with
the exact dataset and access blocker named, and the four arms are defined
with their real status. **Arm A (the control) is implemented and tested**
(`eval/arms/a/schedule.py`, `tests/eval/test_arm_a_schedule.py`) — a fixed
schedule needs no model, so it's the one arm buildable in isolation.

**Update, 2026-08-31 — built**: this paragraph previously named three
blockers (no fitted `p_base`, no LLM call, no persona-simulation engine).
None of the three is accurate anymore. `eval/personas/generator.py`
samples a synthetic population from the fitted Kaggle distributions
(amount shape, dispute rate, the real `p_base` model); `eval/simulate.py`
runs Arms A, B2, and C against that same population and calls the real
`compute_ev()` and `check_bounds()` for Arm C (only Diagnose is simulated —
there's no real text to extract from a synthetic persona). 18 tests
(`tests/eval/test_persona_generator.py`, `tests/eval/test_simulate.py`)
cover reproducibility and the structural invariants (Arm C escalates to a
human in cases the other two structurally cannot; Arm C loses fewer
debtors to contact exhaustion; `EV_FLOOR` genuinely refuses when a
touch's cost dominates the recoverable amount). **What this is not**: a
pre-registered result. `eval/PREREGISTRATION.md` still correctly marks
population size, window length, and the primary comparison metric
`PENDING` — those get committed in their own step, immediately before a
run that counts as evidence, not folded into the commit that built the
code (DEVDOC_v6 §17.6). No arm has been run for that purpose yet. §24.3's
four adversarial personas run against that population, §25's
autonomy/economics reporting (which needs a pre-registered run to have
happened), and §27's vignette study (built and ready to send — needs 25
human respondents, unambiguously a human-input item, not a code gap) all
still follow behind that run.

## No static lint rule against float arithmetic on `Money`

DEVDOC_v6 §9.1 asks for one; this build has a runtime guard
(`agent/money.py::assert_money()`) but not a static analysis rule (would
need a custom mypy or ruff plugin). Noted rather than silently dropped.
