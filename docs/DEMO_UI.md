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
which of the 20 bounds rules passed, which instrument got selected), and
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
   the send itself (with how many of the 20 bounds rules passed and the
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
| Custom recipient | yes — a numeric **chat id**, not a phone number | yes, E.164 | yes, E.164 |
| Reads replies | yes (Telegram's own update feed) | yes (`POST /demo/whatsapp-webhook`, Twilio-signed) | **no** — one-way TTS, no inbound capture |
| Answers replies | yes | yes | n/a |

Guardrails on a caller-supplied number, since honoring one gives up this
endpoint's original "can only ever reach the demo owner" property
deliberately: E.164 format validation, plus a 5-minute per-number cooldown
*on top of* the existing 20-second per-channel one.

### Telegram takes a chat id, and why that is not a way to spam strangers

Telegram addresses *chats*, not phone numbers, so the console has a second
field for it. Entering one does two things: the send goes to that chat, and
that chat is added to a short-lived allowlist so its **replies are answered
too** (`agent/api/demo_allowlist.py`, 6-hour TTL). Without the second half a
judge would receive a message and then be ignored, which is worse than not
offering the field.

The obvious worry is that a free-text chat-id field lets anyone message
anyone. It does not, and the reason is Telegram's own rule rather than
anything here: **a bot cannot message a chat that has not messaged the bot
first.** An id you do not own simply will not accept anything from
`@TrueCommit_bot`. The platform enforces the consent; the allowlist only
decides whose replies get answered.

The inbound guard stays fail-closed. An unknown chat is ignored, and an
unset `DEMO_CONTACT_TELEGRAM_CHAT_ID` with an empty allowlist accepts
nobody — the shape `docs/WHAT_BROKE.md` #25 records getting wrong once.

### "Run the whole thing"

One button fires Telegram, WhatsApp and the call together
(`POST /demo/run-everything`). It calls the same `trigger_demo_contact()`
the individual buttons do — no parallel implementation to drift from them —
and reports each channel separately. **A failure in one does not stop the
others**, because a partial run is far more useful mid-demo than an
exception, and the response names which channels went and which did not.
Telegram is *skipped* rather than redirected when there is no chat id and no
configured contact: quietly falling back to the server's own chat would send
a judge's demo to the demo owner.

### "Sent" is not "arrived"

Twilio accepts a WhatsApp message addressed to a number with no WhatsApp
account. It moves to `sent` and never arrives. The dashboard used to show a
green tick for exactly that, so someone typing a colleague's number saw
success and nothing on the phone, with no way to tell which end was broken.

`GET /demo/message-status?ref=…` returns Twilio's real state, and the
console polls it after every WhatsApp send and call, reporting **Confirmed
on the handset** or **Never arrived** with the error code. `sent` is
deliberately not in `TERMINAL_MESSAGE_STATUSES` — it is precisely where an
undeliverable WhatsApp message comes to rest, so treating it as
terminal-and-successful is the bug rather than the fix.

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

Two shapes come out of a dated promise, and both earn a real mandate:

- **split** -- they named a part-payment ("21,000 on the 5th"). Leg 1 is
  theirs; the balance is proposed a fortnight on and marked
  `proposed_by: "system"`.
- **full** -- they named a date and either the whole balance or no amount
  at all ("I'll pay on the 5th"). That is a one-instalment plan on *their*
  date, and it gets a single e-mandate link. Reading an unstated amount as
  the full balance is the conservative direction: it never quietly reduces
  what is owed.

A promise with no date at all is not a plan. There is nothing to schedule
a debit against, so it takes the ordinary path.

Three things it deliberately will not do:

- **Invent a schedule.** Only the balance leg of a split is ever this
  system's, and it is labelled as such so the reply puts it as a proposal
  rather than reporting it back as agreed.
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

> **Superseded, 2026-09-01.** Polling is no longer how a reply is handled.
> `POST /demo/telegram-webhook` receives the message the moment it is sent,
> and the server answers on its own -- see "Telegram pushes, nothing polls"
> below. This section is kept because the polling path still exists as the
> dashboard's manual "Check now", and because its cost properties are what
> made the webhook worth building.


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

## Telegram pushes, nothing polls

`POST /demo/telegram-webhook` is the difference between a demo that answers
and one that answers only while a browser tab happens to be watching.
Before it existed, a reply sent two minutes after the dashboard's polling
window closed was never handled at all -- the debtor got silence, which is
the single behaviour this project argues hardest against. It is also most
of the latency: polling cost up to a full interval before detection even
began; a push costs nothing.

**Authentication.** Telegram echoes a `secret_token` in
`X-Telegram-Bot-Api-Secret-Token` on every delivery, and it is verified
*before the body is read* -- the same discipline `verify_and_ingest()` uses
for Razorpay, and for the same reason: this endpoint is public, so an
unauthenticated caller could otherwise fabricate a reply and make the
system answer a message the debtor never sent. An unset secret returns 503
rather than accepting unverified deliveries. Setup, and the
`getWebhookInfo` check that catches a mismatch, are in `docs/SETUP.md`.

**Only the configured chat.** A message from any chat that is not
`DEMO_CONTACT_TELEGRAM_CHAT_ID` is acknowledged and dropped, so a stranger
who finds the bot can never surface as though they were the demo's own
debtor.

**It always returns 200.** Telegram retries a non-2xx delivery, and a
payload that failed for a non-transient reason -- unparseable body, wrong
chat -- would fail identically forever. Genuine duplicates are stopped by
the `UNIQUE(conversation_id, external_id)` claim in
`handle_inbound_message()`, not by making Telegram give up. Diagnosis is
cheap to repeat; a *reply* is not free to repeat, and the database decides
that, not application logic.

**One inbound path.** The webhook and the dashboard's manual "Check now"
both call `handle_inbound_message()`, so a message is handled identically
however it arrived, and the claim means only one of them can ever answer
it.

## The e-mandate is real, and it is the point

A plan that ends in a polite sentence is the failure this project exists to
argue against. When `_plan_from_promise()` builds a plan,
`agent/mandate/emandate.py` turns it into **real, authorizable Razorpay
mandates** and the composer is told to include the links verbatim.

Live-verified before anything was built on it (2026-09-01): a real
subscription came back `status=created` with
`short_url=https://rzp.io/rzp/F1Ied9y`, and `start_at` was accepted — so a
mandate really can be scheduled for the date the debtor named rather than
whenever they happen to authorize it.

**One subscription per distinct instalment amount, not one per plan.**
Razorpay's only recurring primitive on this account is Plan + Subscription,
carrying a *fixed* per-cycle amount. A real negotiated split is rarely even
— "21,000 on the 5th and the rest on the 20th" is Rs 21,000 then Rs 21,500
— and one fixed-amount subscription cannot express two amounts. The three
options were:

| Option | What it costs |
|---|---|
| Round the legs into an even division | Misrepresents what the debtor agreed to |
| Authorize at the larger amount | Takes money on the smaller leg they never agreed to |
| One mandate per distinct amount | Two authorizations instead of one |

Only the third is honest, so that is what it does. When the legs *are*
equal — including the common single-leg case, "I'll pay the whole thing on
the 5th" — they collapse into one subscription and the debtor authorizes
once.

The grouping key is `payable_paise`, not `amount_paise`, and that
distinction is a bug this nearly shipped: see WHAT_BROKE #13.

**It is a netbanking/eNACH mandate, and the plan says so.** §12.2's table
recommends `upi_block_reserve_pay` for a single large payment, and this
account has no UPI Autopay approval, so `agent/mandate/rail_capability.py`
substitutes an e-mandate and reports **both** -- the recommendation and
what was issued, with the reason. The spec table is not rewritten; the
filter sits on top of it, and on an approved account nothing would be
substituted. Before this existed the demo reported an instrument it was
not creating.

The authentication method itself is the debtor's choice on Razorpay's
hosted page. `auth_type` is rejected on subscription create for this
account for every value tried -- see `docs/RAIL_CAPABILITIES.md`.

**Nothing here charges anyone.** A `created` subscription is an
authorization *request*. The debtor opens the link and completes
authentication before a rupee moves, and this module never calls a charge
API at all. That is what makes it safe to send the link on a *proposal*:
it hands them the means to say yes, not a debit. The composer is
explicitly told to say authorizing schedules the debit and takes no money
now, and never to imply the payment is done — a false confirmation is the
thing its prompt forbids hardest.

**Scheduled at midday UTC, not midnight.** Midnight UTC on the 5th is the
evening of the 4th in IST — debiting a debtor a day before the date they
named. A leg due sooner than 30 minutes from now is created without a
`start_at` rather than failing: Razorpay refuses a start date that isn't
comfortably ahead, and a mandate they can still authorize beats none.

**Failures are never papered over.** `MandateCreationFailed` is raised, not
a placeholder URL — including when a mandate comes back healthy but with no
`short_url`, which is useless to a debtor who has no way to authorize it. A
partial success across multiple legs raises too: someone handed one of two
links would reasonably believe the whole plan was set up. At the demo
boundary the failure degrades the message (the composer is simply told
there is no link) rather than breaking the exchange. This project shipped
`https://rzp.io/i/pending` to a real person once; the rule since is that an
unavailable link means saying so.

Mandates are cached per plan signature, the same reasoning as
`_last_payment_link_url`: minting a fresh Plan + Subscription on every
message would litter the account with dozens of unauthorized mandates for
one negotiation, and a second different link for the same instalment is
confusing to the person receiving it.

## The case file: what happened, not what your tab saw

The Activity list is a log of what *this browser tab* witnessed. That was
the whole problem. A call placed before the page loaded, or a reply the
Telegram webhook answered while nothing was polling, was invisible — the
system did the work and the UI showed an empty list. A demo that undersells
itself is still a demo that lies about itself.

`GET /demo/timeline` is the server's own record, and the **Case file**
section renders it: a stage track, any live e-mandate links, and every step
in order — sends, calls, replies, diagnoses, decisions, plans, mandates,
refusals. It survives a reload, a cold start, and closing the tab, because
it lives in the same Turso store as everything else.

**The stage** is furthest-reached rather than most-recent — a mandate that
has been issued stays issued even if the next message is small talk. Two
things override that, because both mean automated contact has *stopped*:

| Stage | Meaning |
|---|---|
| `not_started` | Nothing sent yet |
| `contacted` | Sent, waiting on a reply |
| `in_conversation` | Replies being read and answered |
| `negotiating` | A dated instalment plan is on the table |
| `mandate_issued` | A real e-mandate link is theirs to authorize |
| `escalated_to_human` | Overrides progress — a person has it |
| `disputed_paused` | Overrides progress — frozen, chasing stopped |

Reporting "negotiating" over a frozen account would misstate what the
system is doing, which is the one thing this section exists to prevent.

**Unauthenticated, deliberately.** It is a read of the demo's own scripted
invoice and the demo owner's own replies to their own bot. Requiring the
trigger secret would mean baking a send credential into a page that only
wants to watch — a worse trade than publishing a demo transcript. It is
read directly from the backend rather than through the site's serverless
proxy, since there is no secret for a proxy to hold and a public read
doesn't need the extra hop.

The page refreshes it every 5 seconds. That is slow on purpose: replies
arrive by webhook now, so this poll is only catching up a *display*, never
driving the conversation. The difference between those two is exactly what
made the old two-minute polling window a bug rather than a refresh rate.

## Debtor scores, and what they decide

`BoundsContext.debtor.promise_credibility` has been in this system since the
bounds gate was written. `PROMISE_COOLDOWN` scales its grace period by it --
`grace_days * promise_credibility`, in `rules.yaml` and in the
independently-written `human_twin.py` -- and the rule's own comment says the
value is "computed upstream ... from captured payments".

**Nothing computed it.** Every context in the codebase used the `1.0`
default, so a debtor who had broken four promises got exactly the same quiet
time as one who had never broken any. `agent/debtor/` is the missing
upstream half.

**The score is arithmetic over settled facts.** Kept-over-resolved across
the trailing five promises, where *kept* means a rail-confirmed capture
arrived (Law 7's standard, the same one `RecoveryLedger.attribute()`
enforces) and *broken* means the promised date passed without one. A
model's read of how sincere a message sounded never enters it. That is what
makes the score defensible to the person it is applied to, and it is why a
capture settles a promise through the Razorpay webhook rather than through
anything said in the conversation.

Pending promises are excluded rather than counted as failures -- a promise
whose date hasn't arrived is not evidence of anything, and counting it
against someone would penalise them at the moment they made a commitment.

**Four published bands, not a continuous function:**

| Band | Credibility | Grace | Max instalments | Early discount | Statutory interest |
|---|---|---|---|---|---|
| trusted | ≥ 80% | 10 days | 4 | 2% | held |
| standard | ≥ 50% | 7 days | 3 | 2% | held |
| watch | ≥ 25% | 3 days | 2 | 1% | held |
| strict | < 25% | 1 day | 1 (no split) | none | pressed |

Bands rather than a formula because a continuous score would mean every
debtor is offered subtly different terms -- impossible to explain to any of
them, and impossible to audit. Four bands fit on a page.

**A new debtor starts at `trusted`.** Starting everyone at zero would apply
the strictest terms to exactly the people this system knows least about,
which is both unfair and self-defeating: it would refuse the instalment plan
most likely to get a first-time debtor to pay.

**What the score is not allowed to do.** It never changes what is owed. The
MSMED statutory interest rate is set by law and computed by
`agent/statutory/msmed.py`; a band cannot raise it, and nothing here invents
a late fee -- the same prohibition `compose.py`'s prompt and
`payment_plan.py` already carry. A "late fee that gets worse if your score
is bad" would be exactly the invented penalty this project refuses to
produce. The only statutory lever a band has is whether to *press* the claim
or hold it.

The early-payment discount does vary by band, which is a different thing: a
voluntary commercial offer, published per band rather than negotiated per
debtor, so two debtors in the same band are offered identical terms. No band
exceeds the published rate.

**Seeded vs. real.** Four seeded businesses span the bands so the scoring is
demonstrable -- one real debtor with one conversation cannot show a range.
Their histories are *declared*: those promises were never made and never
kept. `is_seeded` is stored per debtor and returned by every API that returns
one, and the UI labels each row, because presenting a fixture as evidence of
real behaviour would be the same overclaim `docs/RESULTS.md` refuses to make
about simulated recovery. The live demo contact is seeded with **no promise
history at all** -- their score is whatever their own replies and payments
earn during a demo.

## Telling the debtor what happened to their payment

The whole conversation is about getting someone to pay, and until now the
moment they did, the system went silent. A debtor who authorizes a mandate
and hears nothing has no way to know it worked. Worse, a debtor whose
payment *failed* is the person most in need of being told: they believe they
have paid, and will not act again until told otherwise.

`payment.captured` and `payment.failed` now produce a message on the channel
the debtor has been talking on. The failure message deliberately makes no
demand and offers no diagnosis -- DIAGNOSE → DECIDE → BOUNDS → ACT is
already running on that same webhook and owns what happens next. It exists
only so the debtor is not left believing a failed payment succeeded, and it
says plainly that nothing was taken from their account.

**Settling is separate from telling.** Whether a payment counts toward a
debtor's record is a fact about the payment, and must not depend on whether
a messaging channel happens to be configured. An earlier version had the
settle *inside* the notifier, so a deployment with no Telegram token
silently stopped scoring altogether -- invisible until someone asked why a
score looked wrong. The capture now settles and records first,
unconditionally; only the send is conditional, and the response says
honestly when nobody was told.

## Three tabs

The console grew past one page. **Live console** holds the three real
channels and the scripted pipeline; **Activity & case files** holds the
session activity, the server-side case file, and the ledger -- the history
view; **Debtors & scores** is the admin view: every debtor, the terms their
record earns, and on selecting one, every promise behind the score next to
the actual conversation that produced it.

The chosen tab is remembered in `localStorage`, wrapped in try/catch because
it throws outright in some embedded contexts.

## Self-service: a debtor asking about their own invoices

The conversation used to be about one hardcoded invoice. `SCENARIOS` held
`INV-2201` and every reply was implicitly about it, so the thing a real
counterparty most often wants could not be asked at all: *which of these do
I still owe?*

Answering that removes work from a human queue without chasing anyone,
which is the same argument this project makes about diagnosis — the
cheapest recovery is the one that never needed a chase.

```
> what do i owe
  1. INV-2201 -- ₹42,500.00 (due, 22 days overdue)
  2. INV-2244 -- ₹9,750.00 (due 2026-09-08)
  3. INV-2176 -- ₹18,400.00 (paid)

  Outstanding: ₹52,250.00.
  Reply with a number to pick one, then: schedule, dispute, or problem.

> 1     → INV-2201 -- ₹42,500.00, 22 days overdue.
> dispute → marked disputed, chasing stopped, routed to a person
```

**Commands are deterministic, not a model call.** `agent/notify/intents.py`
matches a short, unambiguous set — "invoices", a bare number, "dispute",
"schedule", "problem" — and returns `None` for everything else, which falls
through to the real extractor. A menu selection costs nothing and takes no
seconds; routing "2" through a language model costs money, four seconds,
and a chance of being read as something the debtor did not ask for.

**Ambiguity always resolves to prose.** The router caps at 60 characters
and requires a bare number to be the *entire* message, because a number
inside a sentence is an amount or a date. Verified against the cases that
would break it: "2 units were damaged", "I'll pay 2 lakh next week", and a
long message mentioning a dispute all reach the extractor untouched. A
keyword router that swallowed those would be worse than no router.

**Law 8 holds here too.** Matching a keyword decides which of *this
system's* code paths runs. It never lets a message assert a fact. "Mark
INV-2201 as paid" matches nothing in the router and is classified by the
extractor as the claim it is (`ALREADY_PAID_UNRECONCILED`), to be checked
against the rail.

**Status is a fact, not a claim.** `paid` is set only by a rail-confirmed
capture — Law 7's standard, the same one `RecoveryLedger.attribute()`
enforces. A test asserts that four different ways of typing "I've paid it"
leave the invoice `outstanding`.

**A dispute freezes the line.** It is marked `disputed`, drops out of the
outstanding total, routes to a person, and `schedule` on it is refused —
no debit is set up on a contested amount while a human is looking. No
attempt is made to judge whether the dispute is valid; a system deciding
for itself which disputes counted would be doing exactly what
`DISPUTE_FREEZE` exists to prevent.

**Scheduling is deliberately the simplest plan possible** — the whole
invoice on its own due date, as a real mandate. They asked to schedule;
they did not propose terms, and inventing a split would put a schedule in
their mouth. The negotiation path already handles the case where they name
one.

## "Today" is the debtor's today

`date.today()` is the server's local date, and on Render that is UTC. IST
is UTC+05:30, so between 00:00 and 05:30 IST the UTC date is still
yesterday — and for those five and a half hours every relative date the
system resolved was one day early.

Seen live: a debtor wrote "I can pay 21000 today" and the extractor,
told the UTC date, resolved it to 2026-09-01 while their own calendar said
the 2nd. A debit scheduled a day before they agreed to it is a real
problem, not a cosmetic one.

`agent/clock.py::business_today()` now backs every relative date — the
extraction prompt, the promise horizon, the early-payment window, promise
expiry, and seeding. A fixed +05:30 offset rather than a tzdata lookup:
India has one timezone and has never observed daylight saving, so the
offset is exactly correct and has nothing to drift or go missing in a slim
container. `TRUECOMMIT_TIMEZONE_OFFSET_MINUTES` overrides it.

## Confidence gates plan-building

`MIN_CONFIDENCE_FOR_PLAN = 0.65`, calibrated against real extractions
rather than picked round. Messages that genuinely propose a schedule scored
0.90 and 0.93; the two that misled the system scored 0.55 ("either 21000
today or the whole thing on the 10th") and 0.35–0.40 ("make it the 7th
instead of the 5th").

The model was reporting its own uncertainty accurately and nothing was
listening. A plan built on a 0.4-confidence reading gets a real e-mandate
issued against it, which is a strange thing to do with a guess. Below the
threshold the debtor gets an acknowledgement and a question instead of an
instrument.

## Resetting between rehearsals

Rehearsing leaves real marks. A dispute raised in a practice run stays
raised, a scheduled invoice stays scheduled and drops out of the
outstanding total, and the next run opens on last night's leftovers.

`POST /demo/reset` with `DEMO_TRIGGER_SECRET` puts the seeded invoices back
to their declared state. `clear_conversation: true` also wipes the
transcript and timeline -- off by default, because the timeline is the
record of what this system actually did and deleting it is a bigger
decision than putting an invoice back.

```bash
curl -X POST https://<host>/demo/reset   -H 'Content-Type: application/json'   -d '{"secret": "<DEMO_TRIGGER_SECRET>", "clear_conversation": true}'
```

`clear_promises: true` additionally wipes the promise history a score is
computed from. Off by default and separate from `clear_conversation`,
because it deletes a record of things that really happened. It exists
because a defect in matching a capture to a promise (WHAT_BROKE #26) scored
a debtor who had genuinely paid as having broken their word, and a score the
system computed from its own bug has to be correctable.

**What it will not touch:** the recovery ledger, the hash-chained ledger,
and (unless `clear_promises` is set) the promise history behind a debtor's
score. Those record things that
really happened -- a real capture, a real action, a real kept or broken
promise -- and a demo convenience has no business rewriting them. A reset
invoice with a real payment already attributed stays honest that way: the
invoice is outstanding again, and the ledger still says the money moved.

It also clears the in-process rate limiters, or the first message after a
reset would hit a 429 left over from before it.

**`reset_invoices` and `seed_invoices` differ on purpose.** Seeding runs on
every boot and uses `add_if_absent`, so a restart can never resurrect an
invoice a real capture has settled. Reset is a deliberate, secret-gated
call and uses `upsert`, so it can. There is a test for each side of that.

## The refusal strip

Every other surface in this dashboard shows what the agent *did*. The
thesis is that the part worth judging is what it *refuses* to do -- and
until now a refusal rendered like this:

```
Next step decided
no_action · refused by PROMISE_COOLDOWN
```

Small grey text, one row among many, visually indistinguishable from a
successful send. The single most important thing this system does was the
least visible thing on screen.

It now renders as a state:

```
✗  check_bounds   19/20 passed · 1 REFUSED

   PROMISE_COOLDOWN
   A promise buys quiet time. A history of broken promises buys less of
   it, on a sliding scale -- not a hard cutoff -- down to none at zero
   credibility.

   → nothing sent  no_action
```

Three things that line could not do:

**It names the rule and explains it.** `BoundsVerdict.reason` is already
`rule.human` on a refusal, so the plain-language sentence existed the whole
time and was being dropped at the API boundary. The fix was to stop
throwing it away, not to write new copy -- which is why the wording on
screen is the same wording in `rules.yaml`, and stays in sync with it.

**It shows scale.** "1 REFUSED" means little on its own; "19/20 passed"
says a specific gate fired, not that everything failed.

**It shows what happened instead.** A refusal with no stated outcome reads
as the system giving up. The whole design argument is that a refusal is a
*routing decision*, so `→ nothing sent no_action` or `→ handed to a person
escalate_human` sits directly beneath it.

`no_action` also gets its own row treatment. It is described everywhere in
this repo as "a first-class logged decision, not silence", and it was
rendering identically to every other event -- which made it look exactly
like the silence it is supposed to be the opposite of.

A clean pass stays quiet: `✓ check_bounds 20/20 passed`, no body. The
refusal colour is reserved for refusals, so it reads at a glance rather
than competing with a wall of green ticks.

**Back-compatible by construction.** An event recorded before this change
carries no `rules_total`, and `renderGate()` returns an empty string rather
than a broken strip -- so the existing timeline still renders.

## The subscription side: failures predicted, not reported

Every dunning system in existence messages someone *after* a payment fails.
This one runs a deterministic check over each mandate's own fields, finds
debits that **cannot** succeed, and says so while there is still time to fix
them.

That difference is the whole point, and it is a difference in tense.

### What was wrong before this

`check_mandate_health()` had existed, been tested, and produced the
Rs 91,72,435 figure in `docs/evidence/AT_RISK_HEADLINE.md` since early on --
and it was reachable from **no endpoint at all**. An offline tool ran it
once, wrote a markdown file, and that was the entire demonstration of the
project's strongest claim. The "Subscription" demo scenario was a different
message string on the same b2b trigger path; it never touched the detector.

### Six failures, six repairs

`GET /demo/mandate-health` runs the real detector across a seeded book of
eight mandates. Two are healthy -- a detector that flags everything is not a
detector -- and six carry one defect each:

| Defect | The arithmetic | Repair |
|---|---|---|
| `HEADROOM_BREACH` | `max_amount_paise=1800000 < upcoming_debit_paise=2150000` | modify with AFA, or split the debit |
| `EXPIRY_BEFORE_DEBIT` | `end_at < next_debit_date` | re-register ahead of the cycle |
| `AFA_THRESHOLD_BREACH` | `7400000 > 1500000 with no AFA scheduled` | attach AFA to the pre-debit notice |
| `REPEAT_NSF` | `consecutive_nsf=3 >= 2` | re-time the debit |
| `SILENT_REVOCATION` | `status='revoked' with no attempted cycle` | reach out before the missed cycle |
| `RAIL_DEGRADED` | `issuer_failure_rate=0.31 > 0.15` | route to an alternate rail |

One button per defect, because they are genuinely different failures with
genuinely different repairs and a demo that always showed a headroom breach
would undersell five of them.

**The detector's own `detail` string is shown on screen, unedited.** "This
will fail" is worth far more when the reader can check the comparison
themselves -- that is the moment it stops looking like a prediction.

### The warning is the legally required message

`RBI_EMANDATE_PREDEBIT_24H` mandates a pre-debit notice carrying five
specific fields, and `predebit_notice.py` has always built exactly those.
So this is not an extra courtesy bolted on: it is **the compliant
notification, finally carrying something worth reading**. Most systems send
it as noise.

A real alert, live-verified 2026-09-02:

```
Heads up -- your next subscription debit will fail.

Monthly supply plan · Rs 21,500.00 due 2026-09-06 (in 3 days)

Why: the mandate you authorized has a ceiling of Rs 18,000.00, and the next
debit is Rs 21,500.00. The bank will refuse it -- not for want of funds, but
because the authorization itself is too small.

We found this before presenting the debit, so nothing has been declined and
no failed-payment fee applies.

Fix it here: https://rzp.io/rzp/eQPxDR6m
That authorizes a replacement mandate at Rs 25,800.00 -- and you can pick a
different bank account on that page if you'd rather. The old one is
cancelled once this is live. Nothing is charged now.
```

`20/20 bounds rules passed`, a real Razorpay mandate, and a real voice call
(`CA143a95779022b3355ff47949ccf4328e`) telling them to check Telegram.

Four things in that message are deliberate: it names the **arithmetic**, it
says **nothing has been charged** (this is not a failed-payment notice), the
link is **real**, and the correction carries **headroom** -- Rs 25,800 for a
Rs 21,500 debit, so the replacement does not reproduce the defect it exists
to fix.

### "Change your bank", honestly

Razorpay has no API to swap the account behind an existing subscription. It
does not need one: a fresh authorization link lets the debtor pick whichever
account they like on Razorpay's own hosted page, and the old mandate is
revoked once the new one is live. **A new link is the bank change**, done
the only way the rail allows -- and the message says exactly that rather
than implying a capability that does not exist.

### The alert passes the same gate

A warning is an outbound contact like any other. It runs through
`check_bounds()` with all 20 rules, and exempting it because it happens to
be helpful would be exactly the quiet carve-out this project exists not to
have.

### B2B here, and the same mechanism is B2C

The seeded book is **B2B recurring** -- supply plans, retainers, logistics
contracts -- which matches the rest of this project (MSMED, GSTIN, PO
mismatch are all B2B).

**Nothing in the detector is specific to that.**
`max_amount_paise < upcoming_debit_paise` is amount-agnostic arithmetic: a
Rs 499 streaming debit against a Rs 300 ceiling fails in exactly the same
way, and is caught by exactly the same comparison. A consumer subscription
book -- streaming, SaaS seats, gyms, edtech -- would need new seed rows and
no new logic.

One honest caveat if that swap is ever made: `AFA_THRESHOLD_BREACH` only
fires above Rs 15,000, RBI's AFA-free ceiling. At monthly consumer prices it
never triggers, and a purely consumer portfolio would quietly lose one of
the six defect types. Annual renewals cross it, so it survives there.

### Declared, not measured

The defect rates in this book are **declared** -- these mandates were
constructed with breaches, exactly as `AT_RISK_HEADLINE.md` declares its
12%/8%. Nothing here measures how often real Indian subscriptions carry a
headroom breach, and the scan result says so in its own `note` field, which
the dashboard prints.

What is genuinely zero-assumption is the conditional: **given** a mandate
carries a defect, the detector catches it, every time, because it is an
inequality rather than a prediction.

## Two Telegram bots, one person

The subscription demo runs on its own bot, `@Truecommit_subscription_bot`,
so the two conversations are visually separate in Telegram -- which matters
when the demo is being filmed.

**Telegram's private-chat id is the user's id, not a per-bot one**, so both
bots report the same number for the same person. The conversation store
namespaces the subscription thread as `sub:<chat_id>`; without that the two
demos would share a transcript and an outstanding proposal, and a plan
offered by one bot would be acceptable to the other (WHAT_BROKE #24).

Threads are separated. **Identity is not.** A debtor who breaks a promise
about their subscription has broken a promise, and their score should not
reset because a different bot carried the message -- so debtor lookups strip
the namespace (`_channel_ref_of`) while conversation state keeps it.

The Case file has a thread switcher for the same reason: it used to query
the timeline unfiltered, so a mandate warning appeared inside the invoice
story. In "All" mode, subscription rows carry a tag.

Each bot has **its own webhook path and its own secret**
(`/demo/telegram-webhook/subscription`). A shared endpoint with a bot
discriminator would have been less code and a worse trade: when a delivery
starts 403ing, the URL in `getWebhookInfo` should say immediately which bot
broke, and this project has already lost an evening to exactly that
ambiguity.
