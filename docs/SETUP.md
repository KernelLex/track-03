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
(this binds to localhost by default; `--host 0.0.0.0` plus a tunnel like
ngrok, or an actual deployment) and manually configuring that URL and a
webhook secret in the Razorpay dashboard — both outside what this project
can do unattended.

## Environment

- Python 3.12 (pinned via `uv python install 3.12`; the project also runs
  under whatever Python `uv` resolves, but 3.12 is what DEVDOC_v6 §19 targets)
- SQLite (bundled with Python — no server to stand up)
- No Postgres, Redis, Celery, or Node.js required for anything built so far
