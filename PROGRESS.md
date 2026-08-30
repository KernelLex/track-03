# Build progress

Living tracker for the autonomous build. Updated as each piece lands, not
after the fact. See `docs/LIMITATIONS.md` for the honest gaps list this
feeds into, and `eval/PREREGISTRATION.md` for what the simulation run (once
built) commits to measuring.

Status legend: ✅ built & tested · 🔶 built, not yet live-verified · ⬜ not started · ⏳ blocked on you

## Snapshot

| Area | Status | Notes |
|---|---|---|
| Safety/compliance engine (bounds, ledger, state machine, Auditor) | ✅ | 561 tests passing / 11 skipped (live-Razorpay-only) |
| Live Razorpay connection | ✅ | pre-existing, live-verified |
| Telegram channel | 🔶 | `agent/notify/telegram.py` + 12 mocked tests; needs `TELEGRAM_BOT_TOKEN` to live-verify |
| Twilio voice channel | 🔶 | `agent/notify/twilio_voice.py` + 9 mocked tests; needs Twilio credentials to live-verify |
| Claude API extraction (Path B) | 🔶 | `agent/diagnose/llm_extract.py` + 11 mocked tests; needs `ANTHROPIC_API_KEY` to live-verify |
| Monte Carlo simulation harness | ⬜ | not started — next up |
| Full test suite re-run | ⬜ | deferred until the above land, per your instruction |
| Live demo (Demo 1 + Demo 2 from the explainer artifact) | ⬜ | deferred until everything else is built |

## Needs from you

- `ANTHROPIC_API_KEY` — wire into `.env`, live-verify one real extraction call
- `TELEGRAM_BOT_TOKEN` (from @BotFather) — live-verify one real send
- `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / a Twilio "from" number — live-verify one real call
- Still open: keep the `trucommit serve` + cloudflared tunnel running, or restart later?

## Log

- **2026-08-31** — Started this file. Scoped the remaining build: Telegram + Twilio channels (new `agent/notify/` package, since messaging is a distinct concept from the Razorpay `Rail` protocol), the real Claude API call for Path B extraction (`agent/diagnose/llm_extract.py`), then the Monte Carlo harness. Building the credential-shaped code first so live keys can be dropped in and smoke-tested the moment they arrive, rather than waiting idle.
- **2026-08-31** — Built `agent/notify/` (protocol + `SimulatedChannel` + `TelegramChannel` + `TwilioVoiceChannel`), wired an optional `channel` param through `agent/act/executor.py` (additive — every existing caller/test is unaffected), added `"telegram"` to `ALL_CHANNELS`. Built `agent/diagnose/llm_extract.py` (the real Claude call for Path B, `claude-sonnet-5` by default, structured output via `messages.parse(output_format=ExtractionResult)`, reply text kept out of the system prompt per Law 8). Added `anthropic` as a project dependency (also had to bootstrap `pip` into the `uv`-managed `.venv`, which doesn't ship it, and confirmed the SDK's 1.x line uses `httpx2` internally, not `httpx` — matches the `claude-api` skill's own drift warning). 65 new tests, all passing, all against mocks (no real network calls in the default suite, same policy as the existing live-Razorpay opt-in tests).
  - Found and fixed a real bug this exposed: three hardcoded "all channels" test fixtures in `test_bounds_engine.py` assumed a closed 4-channel world and broke the moment a 5th channel was added — fixed to reference `ALL_CHANNELS` directly. Written up in `docs/WHAT_BROKE.md` #6.
  - Updated `docs/LIMITATIONS.md`, `docs/SETUP.md`; added `docs/CHANNELS.md` and `docs/LLM_EXTRACTION.md`; added `.env.example` and placeholder vars in `.env`; added `tools/telegram_get_chat_id.py`.
  - Full suite re-run after every change: 561 passed / 11 skipped.
