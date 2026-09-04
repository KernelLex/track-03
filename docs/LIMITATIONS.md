# Limitations

I'm stating this plainly, per DEVDOC_v6's own standard throughout: name
what's cut, don't bury it.

## What my build is, honestly

I've built a tested, working implementation of TrueCommit's **pure-logic
safety and compliance core** (DEVDOC_v6 §5.2's "the judgment"), **now also
wired to a real, live Razorpay test-mode account** (as of 2026-08-30) for
the capabilities that account actually has, plus a real (if minimal) HTTP
webhook receiver. **1,477 collected / 1,466 passing / 11 skipped as of 2026-09-04**,
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
- ~~A live WhatsApp send.~~ **Done, 2026-09-02.** The Twilio Content
  Template `truecommit_invoice_reminder_v2` came back `approved` (category
  `UTILITY`, no rejection reason), and a real templated cold outbound went
  to a real handset: message `MM8227e461795d36d03ca12dd3e2553ade`,
  polled to a terminal status of **`delivered`** rather than reported at
  `queued`. WhatsApp is now the third live outbound channel.

  Verified on three separate paths, each polled to a terminal status rather
  than reported at `queued`: the raw Twilio API
  (`MM8227e461795d36d03ca12dd3e2553ade`), the agent's own
  `TwilioWhatsAppChannel.send_template()`
  (`MM7185bef793d26eeece03b137aeaf8ac1`), and the deployed Render demo
  endpoint a judge actually clicks (`MM75ba9ac1da51f116b6a81326ef324670`,
  with all 20 bounds rules evaluated including `WHATSAPP_SESSION_WINDOW`).

  **Inbound works — but it did not when this paragraph first claimed it
  did.** Correction, 2026-09-04: for a Twilio WhatsApp sender, inbound is
  routed by the *sender's* callback, not by the phone number's `SmsUrl`. I
  had set `SmsUrl`, watched it read back correctly, and concluded inbound
  worked — while every test I ran was a request I signed myself, which
  proves the handler works and cannot prove Twilio ever calls it. Three
  real messages from a handset produced no timeline entry at all. The
  sender's callback is now set and verified from a real phone. See
  `WHAT_BROKE.md` #31; the general lesson is that testing your own half of
  an integration is not testing the integration.

  **Inbound now works too, as of the same day.**
  `POST /demo/whatsapp-webhook` receives a debtor's reply from Twilio and
  answers it, delegating to `handle_inbound_message()` — the same function
  the Telegram webhook and the dashboard poller already call, so a reply is
  diagnosed, decided, planned and answered identically however it arrived.
  Only three things in that route are WhatsApp-specific: Twilio's signature
  scheme (`agent/notify/twilio_signing.py`, HMAC-SHA1 over the request URL
  plus sorted form params — a completely different scheme from Meta's
  body-HMAC-SHA256), its form-encoded body, and the `whatsapp:` prefix.

  The reply goes out **free-form**, correctly: a debtor who has just
  messaged has opened Meta's 24-hour window, which is exactly the condition
  `WHATSAPP_SESSION_WINDOW` encodes. The template is for the cold open,
  which `/demo/trigger` handles.

  Two things this does *not* cover, stated so the row is not read as more
  than it is. **`agent/notify/whatsapp.py` still has not touched Meta's
  API** — that module is the direct Meta Cloud API path, it remains tested
  only against `httpx.MockTransport` (37 tests), and it is still blocked on
  Meta business verification. Everything live goes through Twilio, a
  different code path. And **a WhatsApp conversation is a different debtor
  record from the same person's Telegram thread** — an E.164 number and a
  Telegram chat id are disjoint address spaces, so credibility built in one
  does not carry to the other. Linking them needs the identity resolution
  this build does not have, the same missing merchant AR lookup that kept
  inbound unwired until now. See `docs/WHATSAPP.md`.

  And — found while running the tests above — **the orchestrated pipeline
  cannot send a template; only the demo path can.** This is about the *cold
  open* only: inbound replies are answered free-form inside the session
  window by the webhook above and are unaffected.
  `agent/act/executor.py`'s message branch calls exactly one method,
  `channel.send(to=..., text=...)`, which is free-form. `send_template()` is
  reached only from `agent/api/demo.py`, which sets
  `uses_approved_template=True` on the bounds context and supplies the
  `ContentSid`.

  This is a capability gap, not a silent failure. If `run_pipeline` chose a
  cold WhatsApp send outside the 24-hour window, rule 20
  (`WHATSAPP_SESSION_WINDOW`) would **refuse it** rather than hand it to a
  channel that would drop it with error 63016 — the gate doing precisely its
  job. But it means autonomous cold WhatsApp outreach is not currently
  possible: ACT has no template path to fall back to once the gate refuses
  the free-form one. Closing it needs a payload carrying `content_sid` and
  its variables, a `MessageChannel` protocol that admits template sends
  without forcing every channel to implement one, and `orchestrate`
  populating `uses_approved_template` so the gate sees the truth. That is
  real work and is deliberately not being rushed in at submission time. The
  judge-facing demo path is unaffected and fully live.

## `BoundsContext.now` is a fixed date, not the clock

`BoundsContext.now` defaults to `datetime(2026, 1, 1)`, and the demo paths
that build a context (`_bounds_context_for`, `_decide_next_step`) do not
override it. Every time-based rule is therefore evaluated against a frozen
instant rather than the real one.

Found while fixing `WHAT_BROKE.md` #30, and deliberately left alone in that
commit. It is not cosmetic: `WHATSAPP_SESSION_WINDOW` currently passes
because `2026-01-01` is earlier than any real inbound timestamp plus 24
hours, which is arithmetically true but is not the question the rule is
asking. `RBI_FPC_HOURS` is the sharper case — at a real clock it would
refuse contact outside 08:00–19:00 IST, and several of this project's own
live sends happened at 20:00 IST and were allowed.

The fix is not a one-liner precisely because it activates several rules
simultaneously, each of which then needs its own live verification. The
bounds engine itself takes `now` from the context and is correct; what is
wrong is what the demo layer puts there. Stated here rather than quietly
carried, because "the gate ran and passed" means less when one of the
gate's inputs is a constant.

## Webhook receiver (§19) — permanently deployed now, not a tunnel

`agent/api/app.py` is a minimal FastAPI app (`POST /webhooks/{source}`,
`GET /health`) I wired directly into `verify_and_ingest()` and
`facts_from_webhook()` — tested end to end through real HTTP
request/response via FastAPI's `TestClient` (`tests/agent/test_api_webhooks.py`,
8 tests, using real `SimulatedRail`-emitted webhooks). **Update,
2026-09-01**: this now runs as a real, permanent deployment on Render
(`https://track-03.onrender.com`) rather than behind an ephemeral
`cloudflared` tunnel — no more "is that specific process pair still alive"
uncertainty. Its ledger is durable across restarts too (`agent/db.py`,
Turso-backed once `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN` are set).
Deploying it live surfaced and fixed a real bug (`verify_and_ingest()` was
reading a real Razorpay webhook's event id from the wrong place entirely —
see `docs/SETUP.md`'s webhook section for the full finding), then proved
the fix against a correctly-signed, real-Razorpay-shaped payload over the
public internet: 200, full unattended DIAGNOSE->DECIDE->BOUNDS->ACT, a real
payment link, a real Telegram send. **Closed, 2026-09-01**: I have now observed actual Razorpay-triggered
deliveries reaching the receiver — real event ids (`TWou1cPf4kddCB`,
`TWou1yfCgyxQHc`), generated by Razorpay, delivered over the public
internet, signature-verified and deduped. See
`docs/evidence/REAL_RECOVERY.md`.

**What changed 2026-09-01: SETTLE is now wired to that path too.** Until
now a real `payment.captured` arriving here would have been ingested,
turned into facts, and then dropped — `_maybe_orchestrate` only ever
looked for a failure code, so the recovery half of the pipeline had no
route from the live webhook at all. That is why `docs/RESULTS.md` can only
call recovery "a modelling convention for my harness": the number came
from the simulation harness, never from the rail. `_maybe_settle`
(`agent/api/app.py`) now attributes a rail-confirmed capture through the
same `RecoveryLedger.attribute()` that enforces Law 7 —
`payment_status == "captured"` or refuse, `UNIQUE(payment_id)` for
count-once, `rail_tag="razorpay"` never "simulated". Tested
(`tests/agent/test_api_settle.py`, 7 tests). **Done, 2026-09-01**: a real
invoice (`inv_TWot3b6dnApicP`, ₹42,500) was paid in test mode, and its
capture was attributed by the deployed service as
`pay_TWotxaQoLsHFOt / rail_tag=razorpay` — read back out of the production
Turso database. That is n=1 against Law 7's real standard rather than the
harness's modelling convention. What is *not* clean about that run,
including a `debtor_id` that fell back to a placeholder because Razorpay
doesn't propagate invoice `notes` onto the payment entity, is written up
in `docs/evidence/REAL_RECOVERY.md` rather than left for a reader to
find. A real
dashboard UI exists too now (`frontend/index.html`, deployed on Netlify —
see `docs/DEMO_UI.md`), not just the bare receiver endpoint.

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
11 tests).

**Update, 2026-09-02 — §17.8's golden set now exists, and its result is
weaker than the headline number looks.** `eval/golden/replies.jsonl` holds
50 labelled replies; the labels are committed in their own commit before the
extractor is run against them, and `eval/golden/score.py` refuses to score if
that ordering cannot be established from git (it also refuses on a shallow
clone, because `git log -1 -- <path>` returns HEAD there — the trap that made
the doc-staleness gate report green for eleven runs, `WHAT_BROKE.md` #22).

The extractor gets **49/50 on class and 50/50 on family**. A keyword baseline
on the same 50 gets **45/50**, so the class-accuracy gap is **not significant
at n=50** (+8.0 pp, p = 0.092). Only family accuracy clears the bar (p =
0.041) — which is the comparison that matters, since family is what gates the
action set, but it is a much narrower claim than "98% accurate".

Three limits worth stating plainly, all of them in
`docs/evidence/EXTRACTION_ACCURACY.md` too:

* **A regex getting 90% means the set is too clean.** These are mostly
  unambiguous exemplars, which is exactly what surface matching handles. The
  set has weak power to discriminate.
* **I wrote 49 of the 50 replies.** One is a real reply harvested from the
  deployed Telegram bot. Authored text carries my own idea of what a debtor
  sounds like; the fix is to keep harvesting live ones.
* **The single miss is arguable and still counts as a miss.** `g048` was
  labelled `MANDATE_INVALID` and read as `SILENT_REVOCATION`, and the
  extractor has a real case. The label stands — moving it after seeing the
  output is the exact thing pre-registration exists to prevent.

Building the baseline is its own entry in `WHAT_BROKE.md` (#27): the first
version scored 94% because I wrote the regexes against my own phrasing, one
of them patching the very blind spot a test item existed to demonstrate.

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

*The golden set itself is built as of 2026-09-02 — see the section above for
its result and its three stated limits. The vignette study is still absent.*

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

## Only netbanking/eNACH mandates are issuable -- UPI Autopay is not approved

`select_instrument()` picks the instrument a plan *should* use, from
DEVDOC_v6 §12.2's table. For a single Rs 42,500 leg that is
`upi_block_reserve_pay`. This account cannot issue that: UPI Autopay needs
explicit Razorpay enablement it does not have
(`docs/RAIL_CAPABILITIES.md`).

`agent/mandate/rail_capability.py` handles this by substituting a
**netbanking/eNACH e-mandate** (Plan + Subscription, the account's only
approved recurring primitive) and reporting *both* facts: the §12.2
recommendation and what was actually issued, with the reason attached. The
AFA-free ceiling is re-derived across the substitution, so an amount over
Rs 15,000 still requires authentication either way.

The spec table itself is untouched. A decision table that bends to
whichever account happens to be configured is not a decision table, so the
capability filter sits on top of `select_instrument()` rather than inside
it -- on an approved account, `UNAVAILABLE_ON_THIS_ACCOUNT` would be empty
and nothing would be substituted.

**What is still a real limitation:** this code cannot pin the
authentication method. Live-probed 2026-09-01, Razorpay rejects
`auth_type` on subscription create for this account for every value tried
(`netbanking`, `debitcard`, `aadhaar`, `nach`, `emandate`, `upi`):
`auth_type is/are not required and should not be sent`. The debtor chooses
their method on Razorpay's hosted authorization page. So "netbanking
e-mandate" names the instrument class this account issues, not a channel
this code selects -- and the two are easy to conflate.

**Previously** this diverged silently: the demo reported
`upi_block_reserve_pay` while creating an e-mandate. Reporting an
instrument you are not creating is a small dishonesty that makes
everything else in a demo suspect, which is why it is fixed rather than
footnoted.

## WHATSAPP_SESSION_WINDOW is a platform rule, not law

The bounds register now has 20 rules. The newest, `WHATSAPP_SESSION_WINDOW`,
models Meta's 24-hour customer-service window: WhatsApp carries a free-form
message only within 24 hours of the debtor's own last inbound one, and
outside that an approved template is the only permitted send.

**It is filed in the `stopping` register, not `regulatory`, deliberately.**
The regulatory register carries `source` and `clause_ref` for statutes this
system must not breach — RBI, TRAI, MSMED. Meta's policy is a vendor's terms
of service. Filing it as regulation would overstate what it is, in exactly
the way this document criticises elsewhere, and a test asserts which
register it lives in so that cannot quietly change.

It is better sourced than most rules here in one specific respect: it was
not only read, it was *hit*. A real send returned Twilio error 63016 —
"failed to send freeform message because you are outside the allowed
window" — during channel bring-up. The rule models a constraint this
project has actually been refused by.

**What adding it exposed.** Nine existing tests failed immediately, and
every one marked a place the code sent on WhatsApp without declaring which
kind of send it was: the demo trigger (an approved template), the
conversational follow-up (free-form, but always answering an inbound
message, so inside the window by construction), and the adversarial
channel-hopper (cold outreach, which can only be a template). The gate had
no way to tell them apart because the callers never said. That is the rule
earning its place before it ever refused anything in production.

**What it still cannot do:** verify that the template being sent is the one
Meta approved. `uses_approved_template` is a caller's assertion, and a
caller that sets it wrongly gets past this gate. Checking it properly means
asking Twilio's Content API for the template's approval status at send
time, which is a network call inside the bounds gate — and a gate that
makes network calls is a gate that can fail open. Not built.

## The mandate book is constructed, and it is B2B

`GET /demo/mandate-health` runs the real detector over eight seeded
mandates. Their defect rates are **declared, not measured** -- the same
status as the 12%/8% construction parameters behind
`docs/evidence/AT_RISK_HEADLINE.md`'s Rs 91,72,435. Nothing in this repo
measures how often real Indian recurring mandates carry a headroom breach,
and the scan response says so in its own `note` field rather than leaving a
reader to work it out.

What is zero-assumption is narrower and worth stating precisely: **given** a
mandate carries a defect, the detector catches it every time, because
`max_amount_paise < upcoming_debit_paise` is a comparison rather than a
prediction. That conditional is the whole claim.

**The book is B2B recurring** -- supply plans, retainers, logistics
contracts -- matching the rest of this project. The detector is not:
`max_amount_paise < upcoming_debit_paise` is amount-agnostic, so a Rs 499
streaming debit against a Rs 300 ceiling is caught by the identical
comparison. A consumer subscription book would need new seed rows and no new
logic.

One caveat if that swap is made: `AFA_THRESHOLD_BREACH` fires only above
Rs 15,000 (RBI's AFA-free ceiling), so at monthly consumer prices it never
triggers and a purely consumer portfolio would lose one of the six defect
types. Annual renewals cross the ceiling and keep it live.

**What the alert cannot verify:** that the replacement mandate it issues is
actually authorized. It creates a real Razorpay subscription and hands over
the link; whether the debtor opens it, and which bank they choose, is
theirs. The old mandate is only revoked once the new one is live -- so a
debtor who ignores the link is left exactly where they were, which is the
correct failure mode but means the repair loop is not closed automatically.
