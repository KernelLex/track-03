# Limitations

Stated plainly, per DEVDOC_v6's own standard throughout: name what's cut,
don't bury it.

## What this build is, honestly

A tested, working implementation of TrueCommit's **pure-logic safety and
compliance core** (DEVDOC_v6 §5.2's "the judgment"), **now also wired to a
real, live Razorpay test-mode account** (as of 2026-08-30) for the
capabilities that account actually has. 499 tests passing with live
credentials present, 489 passing / 10 skipped without (the live suite
skips cleanly — no credentials are required to run the main suite). It is
still **not**:

- A running multi-stage service (no scheduler, no FastAPI dashboard, no
  live webhook receiver process)
- The four-arm evaluation (§17) — no personas, no pre-registration commit,
  no eval harness
- Wired to a real LLM for Path B extraction (§11.2) — no extractor exists

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

**Every `clause_ref` in `agent/bounds/rules.yaml`'s regulatory section is a
literal `TODO` placeholder.** The circular name/date, the ₹15,000 ceiling,
the 24-hour notification window, and the MSMED Act section numbers all come
from DEVDOC_v6 itself — this build did not independently pull and read the
RBI circular or cross-check the MSMED Act's statutory text. Fixing this is
the single highest-value next step for the compliance story: it's reading
one circular and six statutory sections, not new code.

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
are built and tested. §17.8's stratified golden set and §11.7's Auditor
(extractor-drift sampling, bounds-integrity re-check) do not exist yet —
both depend on an actual LLM extractor producing real extractions to sample
and drift-check, and no model call exists anywhere in this codebase.
Chain-integrity, the one Auditor job that needs no model, is fully built
(`Ledger.verify_chain()`).

## EV gate

`p_base` is not fitted (needs the Kaggle IBM Late Payment Histories /
Payment Date Prediction datasets, not pulled into this build), and
`lift_prior` is not typed as `Prior[float]` because `agent/decide/` doesn't
exist yet. `EV_FLOOR`'s bounds rule is ready and tested against a synthetic
`ev_paise` value; nothing yet computes a real one.

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

**Still not built**: §17's experimental design (personas, arms, the
pre-registration + eval harness), §24.3's four adversarial personas run
against a live population, §25's autonomy/economics reporting, and §27's
vignette study. These need, respectively: a persona-simulation harness that
doesn't exist, a recruited human study, and — cutting across all of it —
an actual LLM extractor to generate realistic simulated debtor replies and
to be the thing personas attack. Genuinely large, separate pieces of work.

## No static lint rule against float arithmetic on `Money`

DEVDOC_v6 §9.1 asks for one; this build has a runtime guard
(`agent/money.py::assert_money()`) but not a static analysis rule (would
need a custom mypy or ruff plugin). Noted rather than silently dropped.
