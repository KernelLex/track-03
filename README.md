# TrueCommit

Razorpay AI Buildathon 2026, Track 03 (AI Revenue Recovery). Full spec: [`DEVDOC_v6.md`](DEVDOC_v6.md).

## What this is

A bounded autonomous agent for Indian B2B and subscription revenue
recovery. The part worth judging is not that it acts — it's what it
refuses to do, and that the refusals are checkable by someone who doesn't
trust me.

**Three things that are unusual, in the order I'd want them checked:**

1. **A pre-registration, locked before the run.**
   [`eval/PREREGISTRATION.md`](eval/PREREGISTRATION.md) fixes
   n=500/seed=42/window=30d/lift=1.0 at its own commit *before*
   `eval/report.py` produced [`docs/RESULTS.md`](docs/RESULTS.md) from
   exactly that configuration. The one parameter with no empirical source
   (`lift_prior`) turned out not to decide the outcome — which is a result
   I could only report honestly *because* the analysis was fixed in
   advance.
2. **A bounds gate that two independent implementations agree on.** 19
   rules in YAML, plus `human_twin.py` — a second implementation of the
   same intent, written by hand, deliberately not sharing code — and a
   Hypothesis differential test driving 5,000 generated cases through both
   to prove they agree. `no_action` is a logged, first-class decision, not
   silence.
3. **Every action goes through one chokepoint.** `check_bounds()` before
   any rail call, claim-then-act idempotency in the database rather than in
   application logic, and a hash-chained ledger that `verify_chain()` can
   re-derive. `dry_run=True` runs the identical judgment against 500
   invoices with zero rupees able to move
   ([`docs/evidence/DRY_RUN_BATCH.md`](docs/evidence/DRY_RUN_BATCH.md)).

## The at-risk number, stated precisely

**₹91,72,435 of upcoming debits in a 1,000-mandate batch are structurally
unpayable** — the mandate's own headroom is smaller than the debit it must
cover, or it expires before that debit falls due. `check_mandate_health()`
finds 191 of 191, with no model and no persona involved.

Being precise about what that does and doesn't show: **the detection is
arithmetic, not prediction** — `max_amount_paise < upcoming_debit_paise`
is a comparison, and it is exactly right by construction, which is the
point (a defect you can prove from the object's own shape needs no
behavioural assumption at all). It is *not* evidence of a clever detector:
I generated that batch with a declared 12% headroom-breach and 8%
expiry-breach rate, so the rupee figure is a direct consequence of
parameters I chose. What it demonstrates is that this class of failure is
visible *before* the debit is presented rather than after it fails. Full
construction and caveats:
[`docs/evidence/AT_RISK_HEADLINE.md`](docs/evidence/AT_RISK_HEADLINE.md).

## The comparative claim

At a **neutral** assumption (`lift_prior=1.0`, no assumed behavioural
uplift), the gated pipeline (Arm C) recovers more than both an ungated
fixed schedule and an ungated policy-aware chaser, **while committing zero
real `check_bounds()` violations against hundreds for the two ungated
arms**. A stress-tested elevated touch cost does produce a genuine
break-even τ≈0.49, near the low end of the declared sweep range.

**What this is not:** recovery *in that comparison* is scored against the
simulator's own ground truth — a modelling convention, not Law 7's
rail-confirmed-capture standard, and `docs/RESULTS.md` says so in its own
words. Read the arms as a measurement of bounded execution, not of money.

**Against the real standard, separately: n=1.** A real invoice
(`inv_TWot3b6dnApicP`, ₹42,500) was paid in Razorpay test mode; the
capture arrived as a real Razorpay-triggered webhook and was attributed by
the deployed service as `pay_TWotxaQoLsHFOt`, `rail_tag=razorpay`. One
payment is not a recovery rate — but it is the difference between a
pipeline that models settlement and one that has actually done it. Full
detail, including what was *not* clean about that run:
[`docs/evidence/REAL_RECOVERY.md`](docs/evidence/REAL_RECOVERY.md).

| Real / simulated | What's true right now |
|---|---|
| **rail-confirmed** | `orders`, `payment_links`, `invoices`, `plans`+`subscriptions` (create and revoke), and `settlements` are live-verified against a real Razorpay test-mode account. A real e-mandate (Subscription) with its real customer-facing authentication link, a real reissued invoice, and a real `check_bounds()` refusal of a mandate on a disputed invoice are all in [`docs/evidence/REAL_SCENARIOS.md`](docs/evidence/REAL_SCENARIOS.md). `present_debit`/`modify_mandate` honestly raise "not verified" against the live rail rather than guess — see `docs/LIMITATIONS.md` for the real structural finding (a Subscription bills a fixed amount on its own schedule, not a variable eNACH/UPI-Autopay-style mandate) |
| **simulated-rail** | The ledger, bounds gate, debtor state machine, instrument selection, mandate health + full repair-notify-present-capture lifecycle, MSMED statutory module + early-payment discount, reversal path, and the Auditor's three jobs are all built and tested against `SimulatedRail` and pure logic |
| **simulated-response** | The synthetic Monte Carlo comparison (`docs/RESULTS.md`) and the three adversarial-persona exploits (`docs/evidence/ADVERSARIAL_PERSONAS.md`, 0 cases permanently stalled across 300 runs) both measure the real pipeline's logic against known, synthetic ground truth — not real debtor behaviour |

**A real, live pipeline runs unattended.** A `payment.failed` webhook triggers DIAGNOSE → DECIDE → BOUNDS → ACT automatically — no manual call, no dashboard click — live-verified against the actual running server: a real Razorpay payment link created, a real Telegram message sent with it. `dry_run=True` proves the identical real pipeline's judgment against a batch of invoices with zero rupees able to move — see [`docs/ORCHESTRATION.md`](docs/ORCHESTRATION.md) and [`docs/evidence/DRY_RUN_BATCH.md`](docs/evidence/DRY_RUN_BATCH.md) (500 decisions, spanning all 29 real diagnosis classes, 0 rail calls).

## Thesis

**Razorpay's retry logic asks "can this payment succeed if I try again?"
TrueCommit asks "why did this money not move, and is a payment attempt
even the right action?"** Most overdue B2B money isn't a willingness
problem — it's stuck behind a wrong GSTIN, a PO mismatch, or a payment
already made and never reconciled — and no amount of retrying or
reminding fixes any of that. When it *is* a willingness problem, the fix
is choosing the right payment instrument (mandate, block, link, or a
voluntary early-payment discount) for the amount and authentication
regime, not sending another message.

## What makes this different from a dunning bot

1. **A three-family diagnosis**, not a one-size message: instrument
   failure, administrative blocker, and liquidity/willingness get
   different actions, not the same reminder.
2. **Instrument conversion**: a stated intent becomes a mandate, a block,
   a link, or a discount offer — the correct one for the amount, chosen by
   a pure, ₹15,000-boundary-tested function (`agent/mandate/instrument.py`).
3. **`no_action` is a logged, first-class decision**, not silence — the
   bounds gate's `EV_FLOOR` rule exists specifically so "doing nothing" is
   an auditable choice, not an omission.
4. **It negotiates, and the negotiation ends in a real instrument.** "I can
   do 21,000 on the 5th and the rest later" becomes a dated, priced plan
   (`agent/mandate/payment_plan.py`) and then **real, authorizable Razorpay
   e-mandate links** (`agent/mandate/emandate.py`) — scheduled for the date
   they named, priced at what they were quoted, and charging nothing until
   they authorize it themselves. A bot that answers a payment offer with a
   polite sentence has done the hard part and dropped it.

## Run it yourself

```
uv sync
uv run trucommit demo     # a small, real, end-to-end walk of one debtor
uv run pytest             # 1,007 collected: 996 run without credentials, 11 skipped (they run live with Razorpay test keys set)
```

CI runs that same suite on every push (`.github/workflows/ci.yml`), on
Linux, with no credentials in the environment — plus a second job that runs
it in randomised order to catch order-dependent tests. An external audit
caught this repo with no CI at all, which for a project about gates that
refuse was a contradiction worth naming: `docs/WHAT_BROKE.md` #8.

Reproduce the evaluation and evidence exactly:

```
uv run python eval/report.py                      # docs/RESULTS.md, from the locked pre-registration
uv run python tools/compute_at_risk_headline.py    # docs/evidence/AT_RISK_HEADLINE.md
uv run python tools/run_adversarial_personas.py    # docs/evidence/ADVERSARIAL_PERSONAS.md
uv run python tools/run_dry_run_batch.py           # docs/evidence/DRY_RUN_BATCH.md
```

See [`docs/SETUP.md`](docs/SETUP.md) for the verified clean-clone timing
and [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) for the complete,
itemized list of what's cut and why.

## Positioning

| Razorpay already does | Where it stops |
|---|---|
| Smart retries on failed recurring charges | Operates on the payment object — can't fix a wrong GSTIN, because the blocker lives in the invoice artifact, not the payment |
| Subscription dunning emails | One-way — no view of the reply thread where the buyer says "PO mismatch" |
| Payment link reminders | A reminder doesn't convert a stated commitment into an instrument |

## Deployment

The backend now runs permanently on Render
(`https://track-03.onrender.com`), not a laptop behind a tunnel — a real
Razorpay webhook is registered against it, and deploying it live caught
and fixed a real bug (`agent/ingest/webhooks.py` was reading a webhook's
event id from the wrong place for Razorpay's actual payload shape; see
`docs/SETUP.md`). Its ledger is durable across restarts via Turso
(`agent/db.py`). The demo dashboard is a real, repo-tracked frontend
(`frontend/index.html`) deployed on Netlify at
[truecommit.netlify.app](https://truecommit.netlify.app/), talking to that
backend through a Netlify Function that keeps the demo's trigger secret
server-side rather than shipping it to every visitor — see
`docs/DEMO_UI.md` for the full architecture.

## What's still open

I'm blocked on things outside my own code, not on more of it: the Twilio
WhatsApp sender is genuinely live now (a real send was accepted and
routed — see `docs/CHANNELS.md`'s Twilio section), but a real Content
Template is still needed for a cold outbound send, since a debtor won't
have messaged first — the same platform rule every WhatsApp provider
enforces, not an account restriction. The direct Meta integration
(`agent/notify/whatsapp.py`, see [`docs/WHATSAPP.md`](docs/WHATSAPP.md))
remains its own separate path, blocked on Meta's own business
verification. Also still open: my Razorpay test account's own live-rail
ceiling on `present_debit`/`modify_mandate`. See `docs/LIMITATIONS.md` for
the complete list, including what I deliberately scoped out (the
25-respondent vignette study, §27) rather than left undone by accident.

Full positioning, architecture, the bounds register, regulatory mapping,
and every other Tier-1 document: see [`docs/`](docs/).
