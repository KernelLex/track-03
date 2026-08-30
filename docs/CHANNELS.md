# Messaging channels

How a message-only action (`SEND_REMINDER`, `SEND_PREDEBIT_NOTICE`, ...)
actually reaches a debtor, once `check_bounds()` has approved it. This is a
separate concern from `agent.rails` — a `Rail` creates and mutates Razorpay
payment objects; a `MessageChannel` (`agent/notify/`) only ever sends text.
Neither protocol satisfies the other, and ACT (`agent/act/executor.py`)
takes an optional `channel` argument precisely so a message-only action
still passes through the *same* `check_bounds()` call, the same
claim-then-act idempotency, and the same ledger write as every other
action — there is no lighter-touch path for messages.

## Why Telegram, not SMS/WhatsApp

Both cost money per message through Razorpay/Twilio/a WhatsApp BSP. A
Telegram bot is free to message, with no per-send cost and no approval
wait — `agent.bounds.context.ALL_CHANNELS` now includes `"telegram"`
alongside the DEVDOC_v6-original `sms`/`email`/`whatsapp`/`ivr`, and every
rule that iterates `ALL_CHANNELS` (`OPTOUT`, `CHANNEL_EXHAUSTION`) picked it
up automatically because both the machine rule and the independently
hand-written `human_twin.py` reference the same constant rather than a
hardcoded list (see `docs/WHAT_BROKE.md` #6 for the fixture bug this
constant discipline caught).

**The real constraint this creates**: unlike SMS, a bot cannot cold-message
an arbitrary phone number. The recipient has to have messaged the bot first
(or been added to a group it's in) before a `chat_id` exists to send to.
For a demo this means: message the bot from your own account before the
live run, and use `tools/telegram_get_chat_id.py` to find the `chat_id` to
use as `payload["to"]`. This is a real limitation of the free channel, not
a bug — documented rather than worked around, same policy as every other
honestly-scoped gap in `docs/LIMITATIONS.md`.

## Why Twilio only for voice, not messaging

Twilio SMS/WhatsApp cost per message with no meaningfully free tier;
Telegram covers free messaging instead. Voice calls (the `"ivr"` channel)
have no free equivalent, so Twilio is used there and *only* there —
`agent/notify/twilio_voice.py`'s `TwilioVoiceChannel` implements the same
`MessageChannel.send(to, text)` shape as Telegram, speaking `text` via
Twilio's `<Say>` text-to-speech rather than requiring a hosted TwiML URL,
so a call needs nothing but account credentials and a phone number.

Implemented as raw REST over `httpx` (already a project dependency)
instead of the `twilio` SDK — the surface needed is one endpoint
(`POST .../Calls.json` with inline TwiML), and the project's stated
environment policy (`docs/SETUP.md`) is already "no dependency beyond
what's actually exercised."

## Status

| Channel | Module | Tested | Live-verified |
|---|---|---|---|
| Telegram | `agent/notify/telegram.py` | ✅ mocked (`tests/agent/test_notify_channels.py`) | ✅ 2026-08-31 — see below |
| Twilio voice (`ivr`) | `agent/notify/twilio_voice.py` | ✅ mocked | ✅ 2026-08-31 — see below |
| Simulated | `agent/notify/simulated.py` | ✅ | n/a — never touches the network by design |
| SMS / email / WhatsApp | not implemented | — | — (a Twilio WhatsApp sandbox number is sitting unused in `.env` — not built, see note below) |

"Tested" means the request shape (URL, body, form/JSON encoding) and every
response path (success, a clean API-level rejection, a network failure) are
asserted against `httpx.MockTransport` — no real network call happens in
the default test run, matching this project's existing policy for
`RazorpayRail` (`tests/agent/test_razorpay_rail_live.py` is opt-in only).

## Live verification, 2026-08-31

Run via `uv run python tools/verify_credentials.py` (never prints a secret
value):

- **Telegram — confirmed.** `TelegramChannel.get_me()` returned the real
  bot's identity. No `chat_id` was available yet at verification time (the
  account hasn't messaged the bot) — `send()` itself (an actual message,
  not just the identity check) is still unverified live; see
  `tools/telegram_get_chat_id.py` for the missing step.
- **Twilio — confirmed.** `TwilioVoiceChannel.verify_credentials()`
  authenticated successfully and returned the account's `friendly_name`
  and `status=active`. This is deliberately the read-only account-fetch
  endpoint, not a real call — no phone rang, no cost was incurred.
  Authenticated via a Twilio **API Key** (`TWILIO_API_KEY_SID`/
  `TWILIO_API_KEY_SECRET`), not the classic Account Auth Token, which
  wasn't provided — this required a real fix, not a workaround:
  `TwilioVoiceChannel` now takes an `auth_username` parameter (the API Key
  SID goes in the HTTP Basic Auth *username*, its Secret in the password;
  `account_sid` still identifies the account in the URL path regardless of
  which auth scheme is used). `send()` (an actual call) is still
  unverified live — it needs a real destination number, which wasn't
  provided, and placing one wasn't done speculatively since it rings a
  real phone and costs real money.

A Twilio WhatsApp sandbox number (`TWILIO_WHATSAPP_FROM`) was included with
the credentials but is intentionally unused — the agreed channel split is
Telegram for messaging, Twilio for voice only. Noted here rather than
silently ignored; ask before a WhatsApp channel gets built on it.

## Wiring a live send

```python
from agent.notify.telegram import TelegramChannel

channel = TelegramChannel(os.environ["TELEGRAM_BOT_TOKEN"])
execute_action(..., payload={"to": chat_id, "text": message}, channel=channel)
```

Omitting `channel` (or leaving `payload` without `to`/`text`) preserves the
original stub behaviour — `message_dispatched=True`, no real send — so
every existing caller and all 561 pre-existing tests keep working
unchanged. A message only actually goes out when both a channel and a
fully-shaped payload are supplied.
