# TrueCommit

Razorpay AI Buildathon 2026, Track 03 (AI Revenue Recovery). Full spec: [`DEVDOC_v6.md`](DEVDOC_v6.md).

**This README does not yet have the headline numbers DEVDOC_v6 §20 asks
for** (the break-even τ, the persona-free ₹-at-risk figure) **because the
eval harness that would produce them honestly doesn't exist yet.** See
"Status" below. Everything stated here is either a design description or a
number produced by code that actually runs, tagged as such — nothing here
is a placeholder dressed up as a result.

## Thesis

**Razorpay's retry logic asks "can this payment succeed if I try again?"
TrueCommit asks "why did this money not move, and is a payment attempt
even the right action?"** Most overdue B2B money isn't a willingness
problem — it's stuck behind a wrong GSTIN, a PO mismatch, or a payment
already made and never reconciled — and no amount of retrying or
reminding fixes any of that. When it *is* a willingness problem, the fix
is choosing the right payment instrument (mandate, block, link) for the
amount and authentication regime, not sending another message.

## What makes this different from a dunning bot

1. **A three-family diagnosis**, not a one-size message: instrument
   failure, administrative blocker, and liquidity/willingness get
   different actions, not the same reminder.
2. **Instrument conversion**: a stated intent becomes a mandate, a block,
   or a link — the correct one for the amount, chosen by a pure,
   ₹15,000-boundary-tested function (`agent/mandate/instrument.py`).
3. **`no_action` is a logged, first-class decision**, not silence — the
   bounds gate's `EV_FLOOR` rule exists specifically so "doing nothing" is
   an auditable choice, not an omission.

## Status (2026-08-30)

| Real/simulated | What's true right now |
|---|---|
| **rail-confirmed** | Nothing yet — `tools/probe_rails.py` has never run against a live Razorpay account (no test keys were available while building this) |
| **simulated-rail** | The ledger, bounds gate, debtor state machine, instrument selection, mandate health, MSMED statutory module, and reversal path are built and tested (334 tests) against `SimulatedRail` and pure logic — see `docs/ARCHITECTURE.md` for exactly what's built vs. pending, module by module |
| **simulated-response** | No persona model or eval harness exists yet (DEVDOC_v6 §17) — there is no ₹ recovery figure to report, real or simulated, honest or otherwise |

Run it yourself:

```
uv sync
uv run trucommit demo     # a small, real, end-to-end walk of one debtor
uv run pytest             # 334 tests
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

Full positioning, architecture, the bounds register, regulatory mapping,
and every other Tier-1 document: see [`docs/`](docs/).
