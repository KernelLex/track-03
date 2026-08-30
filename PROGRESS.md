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
| Monte Carlo simulation harness | ✅ | `eval/personas/generator.py` + `eval/simulate.py`, `trucommit simulate`, 18 tests; not yet a pre-registered run |
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
  - Also installed `uv` properly (it wasn't on PATH all session — had been improvising with the venv's own `python -m pip` after bootstrapping `pip` via `ensurepip`) and regenerated `uv.lock` so the new `anthropic` dependency is actually locked, not just added to `pyproject.toml`. Discovered mid-`uv sync` that the `trucommit serve` process from earlier in the session is still running and holding a file lock on its own console-script exe — real evidence it's still alive, answering one of the two open questions without needing to ask.
- **2026-08-31** — Built the Monte Carlo simulation harness: `eval/personas/generator.py` (samples a synthetic population from the fitted Kaggle parameters — amount shape, dispute rate, the real `p_base` model — plus clearly-labelled declared-prior assumptions for what no dataset covers, e.g. contact tolerance) and `eval/simulate.py` (Arms A / B2 / C over that population; B1 skipped on purpose, matching `eval/PREREGISTRATION.md`'s own "cut B1, never B2" guidance). Arm C calls the *real* `compute_ev()` and `check_bounds()` per touch — not a stand-in — so the comparison measures the actual gate/EV logic's aggregate effect. Wired as `uv run trucommit simulate`. 18 new tests, including structural invariants (Arm C escalates to a human in cases A/B2 structurally cannot; Arm C loses fewer debtors to contact exhaustion; `EV_FLOOR` genuinely refuses when cost dominates recoverable amount — a regression test for a real finding made while building this, see `docs/SIMULATION_HARNESS.md`). Updated `eval/PREREGISTRATION.md` to reflect the harness existing without prematurely locking in the pre-registered run parameters (population size / window / primary comparison stay `PENDING` on purpose — that commitment happens in its own step, right before the first run that counts). Full suite: 579 passed / 11 skipped.
