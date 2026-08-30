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
| Telegram | `agent/notify/telegram.py` | ✅ mocked (`tests/agent/test_notify_channels.py`) | ⬜ needs `TELEGRAM_BOT_TOKEN` |
| Twilio voice (`ivr`) | `agent/notify/twilio_voice.py` | ✅ mocked | ⬜ needs `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`/`TWILIO_FROM_NUMBER` |
| Simulated | `agent/notify/simulated.py` | ✅ | n/a — never touches the network by design |
| SMS / email / WhatsApp | not implemented | — | — |

"Tested" means the request shape (URL, body, form/JSON encoding) and every
response path (success, a clean API-level rejection, a network failure) are
asserted against `httpx.MockTransport` — no real network call happens in
the default test run, matching this project's existing policy for
`RazorpayRail` (`tests/agent/test_razorpay_rail_live.py` is opt-in only).
"Live-verified" will mean one real send confirmed the same way
`docs/RAIL_CAPABILITIES.md` confirmed Razorpay: this doc gets a dated
result appended the first time each credential is actually used, not
before.

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
