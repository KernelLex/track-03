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
| Telegram | `agent/notify/telegram.py` | ✅ mocked (`tests/agent/test_notify_channels.py`) | ✅ 2026-08-31 — real send confirmed, see below |
| Twilio voice (`ivr`) | `agent/notify/twilio_voice.py` | ✅ mocked | 🔶 2026-08-31 — credentials confirmed; real call cleanly refused by Twilio's own trial-account limits, see below |
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

- **Telegram — fully confirmed, including a real send.** `get_me()`
  returned the bot's identity; once the demo owner messaged the bot,
  `tools/telegram_get_chat_id.py` recovered a real `chat_id`, and a real
  message was sent through `/demo/trigger` — `status: "sent"`, a real
  Telegram `message_id` back as `external_ref`, checked against all 19
  bounds rules first. This is the actual send path, not the identity-only
  check.
- **Twilio — credentials confirmed; the real call was cleanly refused by
  Twilio itself.** Authenticated via a Twilio **API Key**
  (`TWILIO_API_KEY_SID`/`TWILIO_API_KEY_SECRET`), not the classic Account
  Auth Token, which wasn't provided — this required a real fix, not a
  workaround: `TwilioVoiceChannel` now takes an `auth_username` parameter
  (the API Key SID goes in the HTTP Basic Auth *username*, its Secret in
  the password; `account_sid` still identifies the account in the URL path
  regardless of which auth scheme is used). A real call was then attempted
  to a real number and Twilio's API answered with a clean, specific
  rejection rather than a network/auth error:
  `"Invalid or disallowed parameters provided -- trial accounts have
  limited parameter access, upgrade your account to unlock full
  functionality."` Most likely cause: Twilio trial accounts can normally
  only call numbers verified in the Console (Phone Numbers -> Verified
  Caller IDs), or the inline `Twiml` parameter this project uses
  specifically is trial-restricted. `TwilioVoiceChannel.send()` handled
  this exactly as designed — a clean `status="failed"` result with the
  real reason in `detail`, not an exception, not a silent false success.
  Finding this live also caught a real gap in `/demo/trigger`'s response
  (it dropped `detail` entirely) and in the dashboard's own JS (it checked
  HTTP status, not the actual send status, so this exact failure would
  have shown as a false "sent" in the UI) — both fixed, see `docs/DEMO_UI.md`.

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
