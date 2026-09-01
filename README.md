# TrueCommit

Razorpay AI Buildathon 2026, Track 03 (AI Revenue Recovery). Full spec: [`DEVDOC_v6.md`](DEVDOC_v6.md).

## Headline

**Rs 91,72,435 in upcoming debits was structurally guaranteed to fail — and the system catches every one of them, with zero model and zero persona involved.** Pure arithmetic on a mandate's own object shape (undersized headroom, an expiry preceding the next debit) — not a prediction about anyone's behaviour. See [`docs/evidence/AT_RISK_HEADLINE.md`](docs/evidence/AT_RISK_HEADLINE.md) for the full breakdown, including what's synthetic about the batch and what isn't (the detection is real; the batch's defect rate is a declared demonstration parameter, stated as such).

The comparative claim, honestly nuanced rather than forced into a single number: at a **neutral** assumption (`lift_prior=1.0`, no assumed behavioural uplift), the gated pipeline (Arm C) recovers more than both an ungated fixed schedule and an ungated policy-aware chaser **while committing zero real `check_bounds()` violations against hundreds for the two ungated arms**. At realistic messaging costs, the one parameter with no empirical source (`lift_prior`) turns out not to decide the outcome at all — a stress-tested elevated cost does produce a genuine break-even τ≈0.49, near the low end of the declared sweep range. Full numbers, methodology, and what this is and isn't a claim about: [`docs/RESULTS.md`](docs/RESULTS.md).

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

## Run it yourself

```
uv sync
uv run trucommit demo     # a small, real, end-to-end walk of one debtor
uv run pytest             # 854 tests, no credentials needed (11 more run live with Razorpay test keys set)
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
