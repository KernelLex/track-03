# The demo dashboard

Published at the URL given in chat (title "TrueCommit Console", favicon 🎛️).
A three-scenario, click-through walkthrough of the real pipeline, ending in
one genuinely live action per scenario.

## What's real vs. scripted, and why

**Scripted, but grounded in real code**: the pipeline walk itself (Ingest
through Settle), the numbers shown at each stage (`p_base`, expected value,
which of the 19 bounds rules passed, which instrument got selected), and
the conversation thread. "Scripted" means the *timing* is client-side
(a `setTimeout` stagger, so the demo is reliable with no network dependency
mid-presentation) — not that the numbers are made up. Every number comes
from `tools/gen_demo_data.py` actually calling `compute_ev()`,
`check_bounds()`, and `select_instrument()` for these exact three
scenarios, printed once and pasted into the page as a JS constant. Building
that script surfaced a real mistake (documented in the commit and in
`tools/gen_demo_data.py`'s own comments): the escalation scenario's first
draft hardcoded `ev_paise=0` for the `escalate_human` action, which
`EV_FLOOR` correctly refused — fixed by computing a real EV for it, the
same as any other action.

**Genuinely live**: the one "🔴 LIVE — send this for real" button per
scenario. Clicking it makes an actual `fetch()` call from the published
page, over the internet, to your own locally-running backend
(`agent/api/demo.py`'s `/demo/trigger`, reached through your `cloudflared`
tunnel), which calls the real `TelegramChannel.send()` or
`TwilioVoiceChannel.send()` — the same code `docs/CHANNELS.md` documents,
not a simulation of it.

## Required setup before the live buttons work

1. `trucommit serve` + the `cloudflared` tunnel both need to be running
   (see `docs/SETUP.md`). Confirmed live as of this session:
   `https://bias-verde-retention-intersection.trycloudflare.com` — this is
   a quick tunnel and *will* change if `cloudflared` restarts; the page's
   settings panel has an editable backend-URL field for exactly this
   reason, saved to that browser's `localStorage`.
2. `DEMO_CONTACT_TELEGRAM_CHAT_ID` and `DEMO_CONTACT_PHONE_NUMBER` need
   real values in `.env` (server restart required to pick them up) —
   without them, the endpoint refuses with a clear 503 rather than
   guessing a recipient.
3. Open the dashboard, expand "Demo connection settings," paste in the
   backend URL and your `DEMO_TRIGGER_SECRET` (from `.env`). Saved only in
   that browser's `localStorage` — never part of the page's own published
   source.

## The security tradeoff, stated plainly

`/demo/trigger` is reachable by anyone who has both the tunnel URL and the
secret — and the secret, once *you* paste it into the page, lives in your
browser's local storage, not in the page's source, so someone else opening
the same published link without the secret gets a clean 403. The real
bound on the blast radius isn't the secret, though — it's that the
endpoint can only ever contact the two `DEMO_CONTACT_*` values configured
on your own server, never a caller-supplied recipient, and it's
rate-limited to one trigger per channel per 20 seconds. Worst case if the
link and secret both leaked: someone could repeatedly message or call
*you* while your tunnel is up, not anyone else. Turn it off by stopping
`trucommit serve` (or unsetting `DEMO_TRIGGER_SECRET`) once you're done
demoing.

## Reactive Telegram: polling for a real reply

After a live Telegram send, the dashboard now polls `/demo/check-reply`
every 3 seconds (up to ~2 minutes, or "Stop waiting"/"Check now" on
demand). Reply on your actual phone, and the dashboard: appends your real
message to the conversation thread, runs it through the real
`extract_from_reply()` (an actual, budget-tracked Claude call), and shows
the live diagnosis (family / class / confidence).

**Cost is bounded by construction, not a rate limit.** `after_update_id` is
round-tripped by the client and passed straight to Telegram's `offset`
semantics — a poll that finds nothing new costs nothing (confirmed live:
polling twice with the same reply returned `has_reply: false` the second
time, no second charge). Only a genuinely new reply ever reaches the real
extractor. Live-tested end to end, 2026-08-31: a real reply ("Send me
link") was correctly classified `family=C, class=STALLING,
confidence=0.35` — a low, honest confidence for a genuinely ambiguous
one-liner, not a confident wrong guess.

**Only the configured chat is ever considered.** `/demo/check-reply`
filters every update to `DEMO_CONTACT_TELEGRAM_CHAT_ID` before doing
anything else — a stranger messaging the bot mid-demo can never surface as
if they were the demo's own debtor.

**What's still scripted with no live equivalent**: Twilio calls are
one-way TTS in this build — there's no inbound response capture for a
voice reply, only for Telegram text.
