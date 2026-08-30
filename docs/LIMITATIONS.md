# Limitations

Stated plainly, per DEVDOC_v6's own standard throughout: name what's cut,
don't bury it.

## What this build is, honestly

A tested, working implementation of TrueCommit's **pure-logic safety and
compliance core** — the parts DEVDOC_v6 §5.2 calls "the judgment," which
need no Razorpay test-mode credentials, no LLM API key, and no external
dataset to build or verify. 334 tests passing. It is **not**:

- A running multi-stage service (no scheduler, no FastAPI dashboard, no
  live webhook receiver process)
- The four-arm evaluation (§17) — no personas, no pre-registration commit,
  no eval harness
- Wired to a real Razorpay account (§5, §6) — the day-zero probe
  (`tools/probe_rails.py`) has never executed against live test keys
- Wired to a real LLM for Path B extraction (§11.2) — no extractor exists

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

## What §5.4's conformance suite would and wouldn't prove — not built yet

There is no shared conformance test suite running the same assertions
against both `SimulatedRail` and a real `RazorpayRail` yet
(`agent/rails/conformance/` is an empty package). When built, remember its
scope is narrow by design (§5.4): object shape, state transitions, error
vocabulary, webhook structure, idempotency — never NACH return timing, real
failure distributions, or issuer-specific behaviour, because the reachable
CRUD surface on an unactivated account has no analogue for any of those.

## Golden set, extractor, Auditor

None of §11.2 Path B, §17.8's stratified golden set, or §11.7's Auditor
(extractor-drift sampling, bounds-integrity re-check) exist yet — they all
depend on an LLM extractor that hasn't been built. Chain-integrity, the
one Auditor job that needs no model, is fully built (`Ledger.verify_chain()`).

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

None of DEVDOC_v6 §17 (experimental design), §24 (injection corpus,
adversarial personas), §25 (autonomy/economics reporting), or §27
(vignette validation) has any code yet. These are large, separate pieces
of work — persona modeling, an LLM-in-the-loop extractor to attack, and a
recruited human study — genuinely out of scope for what a single build
session without external credentials, datasets, or study participants can
produce.

## No static lint rule against float arithmetic on `Money`

DEVDOC_v6 §9.1 asks for one; this build has a runtime guard
(`agent/money.py::assert_money()`) but not a static analysis rule (would
need a custom mypy or ruff plugin). Noted rather than silently dropped.
