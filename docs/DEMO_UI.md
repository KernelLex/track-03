# The demo dashboard

Published at [truecommit.netlify.app](https://truecommit.netlify.app/)
(title "TrueCommit Console", favicon 🎛️). A three-scenario, click-through
walkthrough of the real pipeline, ending in one genuinely live action per
scenario. Originally an Artifact-only page; now a real, repo-tracked
frontend (`frontend/index.html`), deployed on Netlify, talking to a
permanently-running backend on Render — no laptop, no tunnel, no manual
credential entry required from anyone who opens the link.

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
Netlify (frontend/index.html)          <- anyone opens this URL
        |  fetch() calls, in-browser
        v
Netlify Function (netlify/functions/*) <- runs server-side, holds the
        |                                  real secret in its own env vars
        v
Render (https://track-03.onrender.com) <- FastAPI backend, always-on
        |                    |
        v                    v
   Turso (ledger,      Real Razorpay / Telegram / Twilio APIs
   durable across
   restarts)
```

- **Netlify** serves the static page — no build step, no secrets in its
  source (`netlify.toml`'s `publish = "frontend"`).
- **Render** replaced the laptop-plus-tunnel: a real, permanently-running
  deployment (see `docs/SETUP.md`'s webhook section for how the
  `payment.failed` -> live-orchestration path was proven against it) with
  its own durable ledger on Turso (`agent/db.py`), so a restart no longer
  loses state the way a local SQLite file behind a dead tunnel would have.
- **Netlify Functions** (`demo-trigger.js`, `demo-check-reply.js`) are the
  actual fix for the secret-handling problem the first version had: a
  Function's env vars run server-side on Netlify's own infrastructure and
  never ship to a visitor's browser, unlike anything written into a static
  page's own JS. The browser sends only `{ channel, scenario }` (or
  `{ after_update_id, diagnose }` for polling) — no secret at all. The
  Function attaches the real `DEMO_TRIGGER_SECRET` itself before
  forwarding to Render. View-source on the deployed page shows nothing;
  a curl against the Function directly (bypassing the browser) still
  requires knowing you're supposed to hit `/.netlify/functions/demo-trigger`
  in the first place, and gets a same-origin check besides.

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
   **Netlify** environment variables (Site configuration -> Environment
   variables) — these are the Functions' own copies, kept separately from
   Render's on purpose, since a Function's env vars are a different trust
   boundary than the backend's.
3. That's it — no per-visitor setup. The old "paste your backend URL and
   secret" settings panel only asks for a backend URL now (used solely for
   the read-only `/health` connection-status check, nothing sensitive).

## The security tradeoff, stated plainly

`/demo/trigger` is reachable by anyone with the Netlify link — but never
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

After a live Telegram send, the dashboard polls `/.netlify/functions/demo-check-reply`
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

**What's still scripted with no live equivalent**: Twilio calls are
one-way TTS in my build — there's no inbound response capture for a voice
reply, only for Telegram text.
