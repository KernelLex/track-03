# Setup

## The ten-minute promise (DEVDOC_v6 §19)

```
git clone <this repo>
cd track-03
uv sync
uv run trucommit demo
uv run pytest
```

**I verified this end to end** by cloning the committed repository into a
scratch directory and timing it (2026-08-30): `uv sync` completed in
under 2 seconds with a warm local package cache (expect longer — still
well under ten minutes — on a fully cold cache, since it's ~35 small
pure-Python packages), `uv run trucommit demo` ran and printed real
output in under a second, and `uv run pytest` passed the whole suite in
about 27 seconds. (The suite has grown a long way past that run's 334
tests since — see the README for the current figure, which is now checked
by `tests/test_documented_test_counts.py` rather than hand-written.)

If `uv` isn't already on your machine:

```
pip install uv
uv python install 3.12      # pins the exact interpreter this project targets
```

`uv sync` will then create `.venv/` and install everything from
`pyproject.toml` / `uv.lock`, including the project itself in editable mode
(`[tool.uv] package = true`), which is what makes `import agent...` and the
`trucommit` console script both work without any extra `pip install -e .`
step.

## What `trucommit demo` actually does

It is **not** the evaluation — that's a separate artifact, and it exists:
`eval/PREREGISTRATION.md` locks n=500/seed=42/window=30d/lift=1.0 at its
own commit, and `docs/RESULTS.md` is generated from exactly that config by
`uv run python eval/report.py`. This paragraph used to say the
pre-registration hadn't been written yet, which stopped being true and sat
uncorrected (`docs/WHAT_BROKE.md` #10). It's a small, real, honestly-scoped walk of
one synthetic debtor through the pieces I've built: the debtor state
machine, `select_instrument()`, `check_bounds()`, `SimulatedRail`, and the
`recovery_ledger`'s attribution, ending with a verified hash-chained ledger.
Every number it prints comes from actually running that code, not from a
hand-typed transcript.

## Running the test suite

```
uv run pytest                    # the whole suite
uv run pytest tests/agent/test_bounds_differential.py   # the 5,000-example differential test alone (~13s)
uv run pytest -k "not differential"                     # skip the slowest test if iterating quickly
```

## Regenerating documentation

```
uv run python tools/gen_docs.py            # regenerates docs/BOUNDS.md, REGULATORY_MAP.md, LEDGER.md
uv run python tools/gen_docs.py --check    # exit 1 if the committed docs are stale (CI gate)
```

## Running the day-zero rail probe

Requires a free Razorpay test-mode account (no KYC needed — DEVDOC_v6 §5.1):

```
RAZORPAY_KEY_ID=rzp_test_xxx RAZORPAY_KEY_SECRET=xxx uv run python tools/probe_rails.py
```

**I ran this** (2026-08-30) — see `docs/RAIL_CAPABILITIES.md` for the
real, generated results. The account cleared `orders`, `payment_links`,
`invoices`, `customers`, `plans`, `subscriptions`, and `settlements`.

## Running the live RazorpayRail tests

The same two env vars unlock `tests/agent/test_razorpay_rail_live.py` (9
tests) and enable the live half of the conformance suite — skipped
cleanly without them, so this is opt-in, not required:

```
RAZORPAY_KEY_ID=rzp_test_xxx RAZORPAY_KEY_SECRET=xxx uv run pytest
```

Store your keys in a local `.env` (already in `.gitignore` — never commit
it) and `source .env` (bash) or load it however your shell prefers before
running commands. These are test-mode keys — the calls create real objects
in Razorpay's test-mode sandbox (no real money, but real API usage against
your account), so treat them as you would any other credential: don't
paste them into a committed file, a shared terminal log, or a screen
recording for the pitch video.

**Pace live runs.** Test-mode accounts have rate limits, which I observed
directly at the end of this build session: `payment_link.create` started
returning `BadRequestError: Too many requests` after I ran repeated
back-to-back live test runs, while `orders`/`invoices`/`plans`/
`subscriptions` kept working — consistent with a per-endpoint limit, not
an account suspension. Not a code bug (the same call succeeded many times
earlier in the same session). If you hit this, wait a few minutes before
re-running `tests/agent/test_razorpay_rail_live.py` rather than assuming
something broke.

## Running the webhook receiver and the scheduled Auditor

```
TRUECOMMIT_WEBHOOK_SECRET_SIMULATED=your-secret \
TRUECOMMIT_LEDGER_DB=ledger.db \
uv run trucommit serve
```

Starts the FastAPI webhook receiver (`agent/api/app.py`) on
`http://127.0.0.1:8000`. `POST /webhooks/{source}` needs a
`TRUECOMMIT_WEBHOOK_SECRET_<SOURCE>` env var per source (uppercased) or it
refuses the request with a 500 rather than accepting an unverifiable
webhook. Setting `TRUECOMMIT_LEDGER_DB` also starts the Auditor's two
model-free jobs on a schedule (`agent/auditor/scheduler.py`) — chain
integrity every 5 minutes, bounds integrity (10% sample) every 15 — logging
at `CRITICAL` on a trip. Omit it and the server still runs, but logs a
warning that nothing is watching the ledger.

Pointing a real Razorpay webhook at this needs a publicly reachable URL
(this binds to localhost by default; `--host 0.0.0.0` plus a tunnel, or an
actual deployment) and registering that URL with Razorpay.

**Superseded, 2026-09-01: a real deployment now exists.** I first did this
with a `cloudflared tunnel --url http://127.0.0.1:<port>` quick tunnel
(no account needed, unlike ngrok, which now requires signup even for its
free tier) — that history is kept below since the API-shape finding it
surfaced is still true, but the tunnel itself is gone. The backend now
runs permanently on Render (`https://track-03.onrender.com`, free tier,
`.python-version` pinned to 3.12 since Render otherwise defaults to the
newest available and refuses to auto-download a matching interpreter),
with `agent/db.py` giving it a durable ledger on Turso instead of a local
SQLite file that would reset on every restart. The real Razorpay webhook
is registered against this permanent URL, not a tunnel.

**A real, live delivery bug was caught and fixed this way.** Testing the
live endpoint surfaced that `verify_and_ingest()`
(`agent/ingest/webhooks.py`) required a top-level `event_id` field in the
webhook body — which is `SimulatedRail._emit()`'s own synthetic envelope
shape, not Razorpay's real one. Checked against Razorpay's actual webhook
documentation: the real event id only ever arrives via the
`x-razorpay-event-id` **header**, never in the body. Every genuine
Razorpay-delivered webhook would have hit `MalformedWebhook` and been
rejected with a 400 — caught before a real webhook ever needed to expose
it. Fixed by threading an optional `event_id_header` through
`verify_and_ingest()`, preferred over the body field when present.
Regression-tested (`tests/agent/test_ingest_webhooks.py`) and live-verified
against the real Render deployment: a correctly HMAC-signed, real-Razorpay-shaped
payload (`event`/`payload` at the top level, no body `event_id`, the id
supplied only via the header) returned `200`, ran the full
DIAGNOSE->DECIDE->BOUNDS->ACT pipeline unattended, and produced a real
payment link plus a real Telegram send. **What this is and isn't**: this
proves the receiver correctly handles a payload shaped exactly like
Razorpay's real webhooks, signed with the real shared secret, over the
real public internet — it is not yet an *actual Razorpay-triggered* event
(every subscribed event still needs a completed checkout to fire, the same
headless-reachability limit already noted for `create_refund` in
`LIMITATIONS.md`). Registration, endpoint reachability, and correct
handling of Razorpay's real payload shape are all confirmed; a
Razorpay-triggered delivery isn't yet.

**One real API-shape finding worth recording**: the SDK's
`events` parameter is a **dict** (`{"payment.captured": True, ...}`), not
a list — passing a list produces the unhelpful `BadRequestError: Invalid
event name/names: 1, 2, 3, 4` (Razorpay's API read the list's indices as
the "event names"). I confirmed the registration via `client.webhook.all()`
afterward.

## CI

`.github/workflows/ci.yml` runs `uv run pytest` on ubuntu-latest for every
push and pull request, with `ANTHROPIC_API_KEY` explicitly empty — the
default suite has to pass with no credentials at all, which is the same
policy `tests/agent/test_llm_extract.py` states for itself. A second job
re-runs the suite under `pytest-randomly` to surface order-dependent
tests deliberately.

This exists because an external audit reported three failures on a clean
clone that I could not reproduce on mine at any of four commits. Two
machines disagreeing with no CI between them is not a resolvable argument;
the Ubuntu job is what settles it. See `docs/WHAT_BROKE.md` #8.

## Running the Monte Carlo simulation harness

```
uv run trucommit simulate --n 300 --seed 1 --lift 2.0
```

Compares Arms A / B2 / C over a synthetic population — zero real model
calls, ~0.5s for 300 personas. See `docs/SIMULATION_HARNESS.md` for what
this does and doesn't prove (short version: it's a real exercise of
`compute_ev()`/`check_bounds()`, not yet a pre-registered result).

## Wiring the Claude API extractor (Path B)

```
ANTHROPIC_API_KEY=sk-ant-xxx uv run pytest tests/agent/test_llm_extract.py
```

That test file never makes a real call (the client is always mocked) and
passes with no key set at all. To try one real extraction:

```python
from agent.diagnose.llm_extract import extract_from_reply
print(extract_from_reply("we will clear this after the Diwali bonus lands"))
```

See `docs/LLM_EXTRACTION.md` for the model choice, the cost math, and
exactly what "tested" does and doesn't mean here. I haven't wired this
into the live webhook path yet — DIAGNOSE doesn't call this automatically
yet when a webhook carries free text.

## Wiring the messaging channels (Telegram, Twilio voice)

```
TELEGRAM_BOT_TOKEN=xxx uv run python tools/telegram_get_chat_id.py
```

Message your bot first (Telegram bots can't cold-message a chat_id that
hasn't messaged them), then run the above to find the `chat_id` to send to.
Then:

```python
from agent.notify.telegram import TelegramChannel
channel = TelegramChannel(bot_token)
channel.send(to=chat_id, text="Invoice INV-1 is 22 days overdue.")
```

Twilio voice needs three env vars (`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
a `TWILIO_FROM_NUMBER` — a Twilio trial account provides one free, with
~75 free trial minutes):

```python
from agent.notify.twilio_voice import TwilioVoiceChannel
channel = TwilioVoiceChannel(account_sid, auth_token, from_number)
channel.send(to="+91xxxxxxxxxx", text="This is a call about invoice INV-1.")
```

Both implement the same `MessageChannel.send(to, text)` shape and can be
passed straight into `execute_action(..., channel=channel)` — see
`docs/CHANNELS.md` for why I made messaging a separate protocol from
`Rail`, and why I chose Telegram/Twilio-voice over SMS/WhatsApp.
`tests/agent/test_notify_channels.py` covers both against mocked HTTP with
zero network access.

To check whichever of `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, and the
Twilio vars are currently set actually work, without sending a message,
placing a call, or spending more than a few cents:

```
uv run python tools/verify_credentials.py
```

It never prints a secret value — see `docs/CHANNELS.md` and
`docs/LLM_EXTRACTION.md` for what each check does and the real, dated
results.

## Environment

- Python 3.12 (pinned via `uv python install 3.12`; the project also runs
  under whatever Python `uv` resolves, but 3.12 is what DEVDOC_v6 §19 targets)
- SQLite (bundled with Python — no server to stand up)
- No Postgres, Redis, Celery, or Node.js required for anything I've built so far

## Wiring the Telegram webhook (instant replies, no polling)

Without this, a debtor's reply is only handled while a browser tab happens
to be polling. With it, Telegram pushes the message the moment it is sent
and the server answers on its own.

Two halves, and **both must carry the same secret**:

```bash
# 1. On the server (Render -> Environment, or .env locally)
TELEGRAM_WEBHOOK_SECRET=<a long random string>

# 2. Tell Telegram where to push, with that same secret
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook"   -d "url=https://<your-host>/demo/telegram-webhook"   -d "secret_token=$TELEGRAM_WEBHOOK_SECRET"   -d 'allowed_updates=["message","edited_message"]'
```

Verify with `getWebhookInfo` -- it reports delivery failures that are
otherwise completely silent:

```bash
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo"
```

`"last_error_message": "Wrong response from the webhook: 403 Forbidden"`
means the two secrets differ. This is worth checking rather than assuming:
a `403` proves the server holds *a* secret, not the *same* one, and a
trailing character lost while pasting into a host's env-var UI produces
exactly this (docs/WHAT_BROKE.md #15). `drop_pending_updates=true` on
`setWebhook` discards anything queued from before the endpoint existed --
use it on first registration so the agent doesn't answer hours-old messages
as though they were live.

## Environment variables added since the first release

| Variable | Default | What it is |
|---|---|---|
| `TELEGRAM_WEBHOOK_SECRET` | *(unset)* | Shared with Telegram's `setWebhook`. Unset makes `/demo/telegram-webhook` return 503 rather than accept unverified deliveries. |
| `TRUECOMMIT_CONVERSATION_DB` | `conversation.db` | Conversation turns, the outstanding proposal, the handled-message claim table, and the dashboard timeline. |
| `TRUECOMMIT_DEBTORS_DB` | `debtors.db` | The debtor register and promise outcomes -- what `promise_credibility` is computed from. |

Under Turso all three share one database and the paths are ignored
(`agent/db.py`); the distinction is local-file only.

## Keeping the free Render instance warm

Render's free tier spins a service down after ~15 minutes idle, and the next
request pays a 30-50s cold start. Nothing breaks -- Telegram and Razorpay
both retry a failed webhook delivery -- but the first message of a demo is
exactly the one that hits a cold server, which is the worst possible moment
for it.

**Primary answer: an external pinger.** [cron-job.org](https://cron-job.org)
or [UptimeRobot](https://uptimerobot.com), free, hitting
`https://<your-host>/health` every 10 minutes. `/health` touches no rail,
sends no message and costs nothing.

**Backstop: `.github/workflows/keepalive.yml`.** Worth reading the history
here, because the obvious version of this does not work. The first attempt
used `cron: "*/10 * * * *"` and ran **zero times** in the two and a half
hours after it was added, while CI on the same repository ran normally.
Nothing was misconfigured -- public repo, not a fork, Actions enabled,
workflow on the default branch. GitHub's scheduler is explicitly
best-effort ("can be delayed during periods of high load"), and
high-frequency crons are throttled hardest.

The current version asks the scheduler for as little as it can: two
triggers an hour (at :23 and :53, avoiding the top of the hour that
GitHub's own docs call out as the worst time to ask), with each run
pinging in a loop for ~25 minutes. One trigger landing covers the half
hour it belongs to, and a dropped trigger costs a gap rather than the whole
scheme.

**Verify it actually runs**, rather than assuming: Actions tab → "Keep the
demo backend warm" → **Run workflow**. A manual run does a single ping and
finishes immediately, so a green check confirms the workflow is valid. Then
check the scheduled runs appear over the next hour -- a workflow that has
never run is indistinguishable from one that does not exist, and this one
looked finished for a day while doing nothing.

**Before recording a demo**, just open `/health` in a browser a minute
beforehand. One request wakes it, and normal demo traffic keeps it up.

If none of this is worth the bother, Render's Starter tier ($7/month) does
not spin down.
