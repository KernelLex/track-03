# The demo dashboard

Published at [truecommit.netlify.app](https://truecommit.netlify.app/)
(title "TrueCommit Console", favicon 🎛️), and prepped to deploy on Vercel
from the same source (see "The architecture, and why it changed" below
for how one frontend serves both). A three-scenario, click-through
walkthrough of the real pipeline, ending in one genuinely live action per
scenario. Originally an Artifact-only page; now a real, repo-tracked
frontend (`frontend/index.html`), talking to a permanently-running
backend on Render — no laptop, no tunnel, no manual credential entry
required from anyone who opens the link.

## What's real vs. scripted, and why

**Scripted, but grounded in real code**: the pipeline walk itself (Ingest
through Settle), the numbers shown at each stage (`p_base`, expected value,
which of the 19 bounds rules passed, which instrument got selected), and
the conversation thread. "Scripted" means the *timing* is client-side
(a `setTimeout` stagger, so the demo is reliable with no network dependency
mid-presentation) — not that the numbers are made up. Every number comes
from `tools/gen_demo_data.py` actually calling `compute_ev()`,
`check_bounds()`, and `select_instrument()` for these exact three
scenarios, which I printed once and pasted into the page as a JS constant.
Building that script surfaced a real mistake I made (documented in the
commit and in `tools/gen_demo_data.py`'s own comments): the escalation
scenario's first draft hardcoded `ev_paise=0` for the `escalate_human`
action, which `EV_FLOOR` correctly refused — I fixed it by computing a
real EV for it, the same as any other action.

**Genuinely live**: the one "🔴 LIVE — send this for real" button per
scenario. Clicking it makes an actual `fetch()` call from the published
page to a Netlify Function (`netlify/functions/demo-trigger.js`), which
forwards to the real backend's `agent/api/demo.py`'s `/demo/trigger`
(reached at `https://track-03.onrender.com`), which calls the real
`TelegramChannel.send()` or `TwilioVoiceChannel.send()` — the same code
`docs/CHANNELS.md` documents, not a simulation of it. Live-verified end to
end, 2026-09-01: a real Telegram message sent through this exact path,
returning `status="sent"` with a real Telegram message id as
`external_ref`.

**A real payment link, and a real reply back.** The b2b scenario's live
send now includes a real Razorpay payment link
(`agent/api/demo.py::_create_real_payment_link`, the same `RazorpayRail`
the actual orchestration path uses — test-mode, so no real money moves).
Live-caught building this: Razorpay's test-mode account has a hard
**30-payment-link cap**, and creating a fresh one per click burns through
it fast — a session of routine testing had already exhausted it once,
which surfaced as the message silently sending without a link. Two fixes,
because the first one wasn't enough:

1. The trigger creates **one** real payable URL per demo run and reuses it
   on every subsequent b2b click, rather than minting a new one each time.
2. **That cap counts lifetime creates, not live links** — cancelling six
   old probe links freed nothing, and `create_payment_link` on an
   exhausted account can never succeed again. So the fallback is a real
   Razorpay **invoice** (`create_invoice`), which has its own quota and is
   arguably the better object anyway: the scenario *is* an overdue
   invoice, so a real Razorpay-hosted invoice page is what a debtor would
   actually be sent. Both are rail-created and payable; neither is a
   stand-in.

**No placeholder URLs, ever.** An earlier version filled the WhatsApp
template's link variable with `https://rzp.io/i/pending` when link
creation failed — a template variable can't be empty, so *something* had
to go there. That shipped a real message containing a fake-looking link,
which is worse than not sending: WhatsApp now refuses the send outright
(503) when no real payable URL is available. Telegram still degrades
gracefully instead, because its body is free-form and can simply omit the
line.

Replying is no longer a dead end either — `/demo/check-reply` sends a real
message back over the same channel after diagnosing a reply, not just a
diagnosis shown on the dashboard. **Guarded against sending twice**:
diagnosing the same reply repeatedly is harmless, but the follow-up *send*
isn't idempotent by nature, and this endpoint has no session concept to
rely on a client's own tracked position — `_last_followed_up_update_id`
(and `_last_followed_up_whatsapp_sid` for WhatsApp) is the actual guard,
not the caller's good behavior (a page reload resetting the dashboard's
state would otherwise re-trigger a real duplicate send for an old reply).

## The page, as laid out

1. **Live console** (top) — a recipient field and three buttons, one per
   real channel. This is the only place a real send is fired from; the
   scripted walkthrough below it explains the pipeline but no longer
   triggers anything (two places to fire the same real send was worse than
   one). The Telegram card carries its own instructions inline, because
   its constraint is genuinely different from the other two — see the
   table below.
2. **Activity** — every real event, newest first, with a timestamp:
   the send itself (with how many of the 19 bounds rules passed and the
   channel's own reference id), the real reply when it arrives quoted
   verbatim, the extractor's scoring (family, class, and a real
   confidence, not a fixed number), a bounds refusal if the follow-up is
   ever gated, and the composed answer that went back. This is the panel
   to point a judge at: everything in it actually happened, none of it is
   scripted.
3. **Scripted walkthrough** (below) — the three-scenario pipeline
   explainer, unchanged, still driven by real precomputed numbers.
4. **Ledger — this session** — the terse per-stage row view, still fed by
   both the scripted run and the real console events.

## Three real channels, and what each can actually do

| | Telegram | WhatsApp | Voice call |
|---|---|---|---|
| Send | real message | real Content Template (cold sends need one — WhatsApp's rule, see `docs/CHANNELS.md`) | real TTS call |
| Custom recipient | **no** — a bot can only message someone who messaged it first, so there's no number to address; a supplied `to` is refused outright rather than silently ignored | yes, E.164 | yes, E.164 |
| Reads replies | yes (Telegram's own update feed) | yes (polls this Twilio account's own message history — no webhook, so no Console configuration needed for a demo surface) | **no** — one-way TTS, no inbound capture |
| Answers replies | yes | yes | n/a |

Guardrails on a caller-supplied number, since honoring one gives up this
endpoint's original "can only ever reach the demo owner" property
deliberately: E.164 format validation, plus a 5-minute per-number cooldown
*on top of* the existing 20-second per-channel one.

## Negotiating a split, not just asking for the money

"I can do 21,000 on the 5th and the rest later" is the most common useful
reply in collections and the one a dunning bot handles worst: it either
ignores the offer and repeats the full amount, or accepts it with nothing
behind it. `agent/mandate/payment_plan.py` answers it properly, and every
piece it needs already existed -- `select_instrument()`,
`compute_early_payment_offer()`, and `Promise.installments`, which the
extractor has always populated. The module is the join, nothing more: it
computes and returns a plan, and sends or charges nothing.

For Rs 42,500 offered as 21,250 on the 5th and the balance a fortnight on:

```
  1. Rs 21,250 due 2026-09-05 -- Rs 20,825 if paid by 2026-09-11 (saves Rs 425)
  2. Rs 21,250 due 2026-09-20
  Instrument: recurring_emandate_afa_per_debit (AFA required per debit)
```

**The instrument follows the split, and that is the interesting part.**
The AFA-free ceiling is per debit, not per plan. Two legs of Rs 21,250 are
two debits over the ceiling, so every one needs additional factor
authentication. The same total in four legs of Rs 10,625 is under it, and
a single authorization covers the whole plan -- a real argument for
offering a longer split, produced by the existing rules rather than
decided here.

Three things it deliberately will not do:

- **Invent a schedule.** A promise with no amount, or one covering the
  full balance, is an ordinary promise and takes the normal path. Only a
  genuine part-payment offer builds a plan.
- **Round their numbers.** Legs that don't sum to the invoice raise
  `PlanRejected` rather than being quietly adjusted -- a shortfall is a
  real disagreement about what is owed, not a rounding problem, and
  `installment_amount_paise` exists precisely because a real plan need not
  divide evenly.
- **Invent a fee.** A discount is `compute_early_payment_offer()`'s
  published rate against a date the debtor themselves named -- earned, not
  offered as an inducement to move. Late-payment figures come from
  `agent/statutory/msmed.py`'s statutory interest, never a late fee this
  project made up. The composer's prompt already forbids stating a
  consequence, and a fabricated fee is exactly that.

A date the debtor did not name is marked `proposed_by: "system"` and the
composer is told to put it as a proposal to confirm, rather than reporting
it back as though they agreed to it.

## The reply is composed, not canned

The first version answered with one fixed sentence per diagnosis family
("Understood — pausing automated contact on this invoice"). Defensible as
a fallback, but it doesn't *read* what was actually said: a promise of a
specific date, a question about the link, and a flat refusal all got the
same Family C sentence. `agent/notify/compose.py` replaces that with a
real, budget-tracked model call that answers the actual message — the
generating counterpart to the extractor's classifying one.

Two things it deliberately doesn't relax:

- **Law 8** — the debtor's message goes in the user turn, never
  concatenated into the system prompt, exactly as `llm_extract` does it.
  Nothing in a reply can reach the instruction channel, however it's
  phrased.
- **Authority** — the prompt forbids promising a discount, waiver, or
  extension; confirming a payment as received; or stating any consequence
  (legal, credit, fees, suspension). An LLM asked to be helpful reaches
  for exactly those otherwise, and none of them are a message-writer's to
  give. The fixed family line remains the fallback when the composer is
  unavailable — a bland known-safe sentence, never a retry with the
  guardrails loosened.

**The bounds gate runs on the reply too** (`_bounds_gate_followup`), not
just on the outbound trigger. A reply is an outbound contact like any
other; exempting it for being a response would be exactly the kind of
quiet carve-out this project exists not to have.

## The architecture, and why it changed

The first version of this page was published only as a Claude Artifact,
talking to my own laptop through a `cloudflared` quick tunnel — reachable
only while both were running, and requiring anyone using the live buttons
to manually paste in a backend URL and `DEMO_TRIGGER_SECRET` (saved to
that browser's own `localStorage`). That worked for a same-session demo,
but broke the moment I closed my laptop or the tunnel rotated, and put a
real secret one paste away from every visitor's clipboard.

Current setup:

```
Netlify or Vercel (frontend/index.html) <- anyone opens this URL
        |  fetch() calls to /api/*, in-browser
        v
Serverless function (per-platform)      <- runs server-side, holds the
        |                                  real secret in its own env vars
        v
Render (https://track-03.onrender.com)  <- FastAPI backend, always-on
        |                    |
        v                    v
   Turso (ledger,      Real Razorpay / Telegram / Twilio APIs
   durable across
   restarts)
```

**Deployed on both Netlify and Vercel from the same source, deliberately.**
`frontend/index.html`'s JS calls a single platform-neutral path,
`/api/demo-trigger` / `/api/demo-check-reply` — Vercel's own zero-config
convention (`api/*.js` at the project root, `vercel.json`'s
`outputDirectory: "frontend"` for the static page). Netlify doesn't share
that convention (its functions live under `/.netlify/functions/*`), so
`netlify.toml` carries two `[[redirects]]` rewriting `/api/*` to that
location — the frontend itself never needs to know which platform served
it. `netlify/functions/*.js` (Netlify's Node-style `exports.handler`) and
`api/*.js` (Vercel's Web-standard `export async function POST(request)`)
are two separate files implementing the identical proxy, one per
platform's actual function signature — not something I could share as one
file, since the two platforms' handler contracts are genuinely different,
not just differently named.

**Live-caught deploying to Vercel**: its zero-config framework detection
scans the whole repo, not just `frontend/`/`api/` — found `pyproject.toml`
and `agent/api/app.py`'s FastAPI `app`, and tried to deploy that as a
Python/FastAPI project instead of the static-plus-Node-functions setup
actually intended (that backend is already live on Render; Vercel
redeploying it too would be redundant at best). Fixed with
`"framework": null` in `vercel.json` — the documented way to force
Vercel's "Other" preset and disable auto-detection entirely, not
something guessed from the error message alone.

- **Render** replaced the laptop-plus-tunnel: a real, permanently-running
  deployment (see `docs/SETUP.md`'s webhook section for how the
  `payment.failed` -> live-orchestration path was proven against it) with
  its own durable ledger on Turso (`agent/db.py`), so a restart no longer
  loses state the way a local SQLite file behind a dead tunnel would have.
- **The serverless function layer** (whichever platform is serving the
  page) is the actual fix for the secret-handling problem the first
  version had: its env vars run server-side and never ship to a visitor's
  browser, unlike anything written into a static page's own JS. The
  browser sends only `{ channel, scenario }` (or `{ after_update_id,
  diagnose }` for polling) — no secret at all. The function attaches the
  real `DEMO_TRIGGER_SECRET` itself before forwarding to Render.
  View-source on the deployed page shows nothing; a curl against the
  function directly (bypassing the browser) still gets a same-origin
  check besides.

**The one earlier approach I tried and reverted**: hardcoding the secret
directly into the page's JS as a convenience default (so it auto-filled
instead of needing a paste). That's strictly worse than the Function
approach, not a stepping stone to it — a hardcoded value in a public
static file is exactly as visible to every visitor as no gate at all,
just with extra steps. I kept it only long enough to realize the Function
was the actual fix, then replaced it.

## Required setup before the live buttons work

1. `TRUECOMMIT_LEDGER_DB`, `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN`,
   `DEMO_TRIGGER_SECRET`, `DEMO_CONTACT_TELEGRAM_CHAT_ID`, and
   `DEMO_CONTACT_PHONE_NUMBER` all need real values as **Render**
   environment variables (a server restart there is automatic on env var
   save).
2. `DEMO_TRIGGER_SECRET` and `BACKEND_URL` need to also be set as
   environment variables on **whichever platform is serving the page**
   (Netlify: Site configuration -> Environment variables; Vercel: Project
   Settings -> Environment Variables) — these are the function layer's own
   copies, kept separately from Render's on purpose, since a Function's
   env vars are a different trust
   boundary than the backend's.
3. That's it — no per-visitor setup. The old "paste your backend URL and
   secret" settings panel only asks for a backend URL now (used solely for
   the read-only `/health` connection-status check, nothing sensitive).

## The security tradeoff, stated plainly

`/demo/trigger` is reachable by anyone with the deployed link — but never
with a caller-supplied recipient. The endpoint can only ever contact the
two `DEMO_CONTACT_*` values configured on Render, and is rate-limited to
one trigger per channel per 20 seconds. Worst case if the setup is left
running indefinitely: someone could repeatedly message or call *me*
through my own configured contacts, not anyone else, and Telegram sends
cost nothing — the real cost exposure is the Twilio IVR scenario, priced
per call. I can close this off entirely at any time by unsetting
`DEMO_TRIGGER_SECRET` on Render (the Function's forwarded request then
fails auth against the backend, independent of whatever the Function's own
copy still says) or by taking the Render service down.

## Reactive Telegram: polling for a real reply

After a live Telegram send, the dashboard polls `/api/demo-check-reply`
(itself proxying to `/demo/check-reply`) every 3 seconds (up to ~2 minutes,
or "Stop waiting"/"Check now" on demand). I reply on my actual phone, and
the dashboard: appends my real message to the conversation thread, runs it
through the real `extract_from_reply()` (an actual, budget-tracked Claude
call), and shows the live diagnosis (family / class / confidence).

**Cost is bounded by construction, not a rate limit.** `after_update_id` is
round-tripped by the client and passed straight to Telegram's `offset`
semantics — a poll that finds nothing new costs nothing (confirmed live: I
polled twice with the same reply and got `has_reply: false` the second
time, no second charge). Only a genuinely new reply ever reaches the real
extractor. I live-tested this end to end, 2026-08-31: a real reply ("Send
me link") was correctly classified `family=C, class=STALLING,
confidence=0.35` — a low, honest confidence for a genuinely ambiguous
one-liner, not a confident wrong guess.

**Only the configured chat is ever considered.** `/demo/check-reply`
filters every update to `DEMO_CONTACT_TELEGRAM_CHAT_ID` before doing
anything else — a stranger messaging the bot mid-demo can never surface as
if they were the demo's own debtor.

**Update, 2026-09-01: this no longer stops at the diagnosis.** A real
follow-up message now goes back over Telegram after every new reply — see
the "A real payment link, and a real reply back" section above for what
it says and how it's guarded against sending twice.

**What's still scripted with no live equivalent**: Twilio calls are
one-way TTS in my build — there's no inbound response capture for a voice
reply, only for Telegram text.
