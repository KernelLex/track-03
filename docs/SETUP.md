# Setup

## The ten-minute promise (DEVDOC_v6 §19)

```
git clone <this repo>
cd track-03
uv sync
uv run trucommit demo
uv run pytest
```

**This has been verified end to end** by cloning the committed repository
into a scratch directory and timing it (2026-08-30): `uv sync` completed in
under 2 seconds with a warm local package cache (expect longer — still well
under ten minutes — on a fully cold cache, since it's ~35 small pure-Python
packages), `uv run trucommit demo` ran and printed real output in under a
second, and `uv run pytest` passed all 334 tests in about 27 seconds.

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

It is **not** the four-arm evaluation from DEVDOC_v6 §17 — that needs
persona definitions and a committed pre-registration that don't exist yet
(see `docs/LIMITATIONS.md`). It is a small, real, honestly-scoped walk of
one synthetic debtor through the pieces that are built: the debtor state
machine, `select_instrument()`, `check_bounds()`, `SimulatedRail`, and the
`recovery_ledger`'s attribution, ending with a verified hash-chained ledger.
Every number it prints comes from actually running that code, not from a
hand-typed transcript.

## Running the test suite

```
uv run pytest                    # all 334 tests
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

**This has been run** (2026-08-30) — see `docs/RAIL_CAPABILITIES.md` for
the real, generated results. The account cleared `orders`, `payment_links`,
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

**Pace live runs.** Test-mode accounts have rate limits, observed directly
at the end of this build session: `payment_link.create` started returning
`BadRequestError: Too many requests` after repeated back-to-back live test
runs, while `orders`/`invoices`/`plans`/`subscriptions` kept working —
consistent with a per-endpoint limit, not an account suspension. Not a code
bug (the same call succeeded many times earlier in the same session). If
you hit this, wait a few minutes before re-running
`tests/agent/test_razorpay_rail_live.py` rather than assuming something
broke.

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

**Done once, live, 2026-08-31** — registered via the API itself rather than
the dashboard, using `cloudflared tunnel --url http://127.0.0.1:<port>`
(a `trycloudflare.com` quick tunnel — no account needed, unlike ngrok,
which now requires signup even for its free tier) for the public URL, and
`client.webhook.create(...)` for the registration. **One real API-shape
finding worth recording**: the SDK's `events` parameter is a **dict**
(`{"payment.captured": True, ...}`), not a list — passing a list produces
the unhelpful `BadRequestError: Invalid event name/names: 1, 2, 3, 4`
(Razorpay's API read the list's indices as the "event names"). Confirmed
registered via `client.webhook.all()` afterward.

**Caveat**: a `trycloudflare.com` quick tunnel is ephemeral — it dies with
the `cloudflared` process, and Cloudflare states no uptime guarantee even
while it's running. A webhook registered against one will start silently
failing deliveries once the tunnel (or the local server behind it) stops.
For anything beyond a same-session demo, either keep both processes
running for the duration, or register a new webhook (or edit the existing
one via `client.webhook.edit(webhook_id, {...})`) pointing at a real
deployment's URL instead. Real end-to-end delivery (an actual Razorpay-
triggered `payment.captured` reaching this receiver) was **not** observed
in this session — every subscribed event needs a completed checkout
(3DS/OTP) to fire, the same headless-reachability limit already noted for
`create_refund` in `LIMITATIONS.md`. Registration and endpoint reachability
are confirmed; a live-triggered delivery isn't yet.

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
exactly what "tested" does and doesn't mean here. Not yet wired into the
live webhook path — DIAGNOSE doesn't call this automatically yet when a
webhook carries free text.

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
`docs/CHANNELS.md` for why messaging is a separate protocol from `Rail`,
and why Telegram/Twilio-voice were chosen over SMS/WhatsApp.
`tests/agent/test_notify_channels.py` covers both against mocked HTTP with
zero network access.

To check whichever of `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, and the
Twilio vars are currently set actually work, without sending a message,
placing a call, or spending more than a few cents:

```
uv run python tools/verify_credentials.py
```

Never prints a secret value — see `docs/CHANNELS.md` and
`docs/LLM_EXTRACTION.md` for what each check does and the real,
dated results.

## Environment

- Python 3.12 (pinned via `uv python install 3.12`; the project also runs
  under whatever Python `uv` resolves, but 3.12 is what DEVDOC_v6 §19 targets)
- SQLite (bundled with Python — no server to stand up)
- No Postgres, Redis, Celery, or Node.js required for anything built so far
