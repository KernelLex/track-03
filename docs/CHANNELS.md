# Messaging channels

How a message-only action (`SEND_REMINDER`, `SEND_PREDEBIT_NOTICE`, ...)
actually reaches a debtor, once `check_bounds()` has approved it. I treat
this as a separate concern from `agent.rails` — a `Rail` creates and
mutates Razorpay payment objects; a `MessageChannel` (`agent/notify/`) only
ever sends text. Neither protocol satisfies the other, and I gave ACT
(`agent/act/executor.py`) an optional `channel` argument precisely so a
message-only action still passes through the *same* `check_bounds()` call,
the same claim-then-act idempotency, and the same ledger write as every
other action — there is no lighter-touch path for messages.

## Why Telegram, not SMS/WhatsApp

Both cost money per message through Razorpay/Twilio/a WhatsApp BSP. A
Telegram bot is free to message, with no per-send cost and no approval
wait — I added `"telegram"` to `agent.bounds.context.ALL_CHANNELS`
alongside the DEVDOC_v6-original `sms`/`email`/`whatsapp`/`ivr`, and every
rule that iterates `ALL_CHANNELS` (`OPTOUT`, `CHANNEL_EXHAUSTION`) picked
it up automatically because both the machine rule and the independently
hand-written `human_twin.py` reference the same constant rather than a
hardcoded list (see `docs/WHAT_BROKE.md` #6 for the fixture bug this
constant discipline caught).

**The real constraint this creates**: unlike SMS, a bot cannot cold-message
an arbitrary phone number. The recipient has to have messaged the bot first
(or been added to a group it's in) before a `chat_id` exists to send to.
For a demo this means: I message the bot from my own account before the
live run, and use `tools/telegram_get_chat_id.py` to find the `chat_id` to
use as `payload["to"]`. This is a real limitation of the free channel, not
a bug — I've documented it rather than worked around it, the same policy I
apply to every other honestly-scoped gap in `docs/LIMITATIONS.md`.

## Why Twilio only for voice, not messaging

Twilio SMS/WhatsApp cost per message with no meaningfully free tier;
Telegram covers free messaging instead. Voice calls (the `"ivr"` channel)
have no free equivalent, so I use Twilio there and *only* there —
`agent/notify/twilio_voice.py`'s `TwilioVoiceChannel` implements the same
`MessageChannel.send(to, text)` shape as Telegram, speaking `text` via
Twilio's `<Say>` text-to-speech rather than requiring a hosted TwiML URL,
so a call needs nothing but account credentials and a phone number.

I implemented it as raw REST over `httpx` (already a project dependency)
instead of the `twilio` SDK — the surface needed is one endpoint
(`POST .../Calls.json` with inline TwiML), and my stated environment
policy for this project (`docs/SETUP.md`) is already "no dependency beyond
what's actually exercised."

## Status

| Channel | Module | Tested | Live-verified |
|---|---|---|---|
| Telegram | `agent/notify/telegram.py` | ✅ mocked (`tests/agent/test_notify_channels.py`) | ✅ 2026-08-31 — real send confirmed, see below |
| Twilio voice (`ivr`) | `agent/notify/twilio_voice.py` | ✅ mocked | 🔶 2026-08-31 — credentials confirmed; real call cleanly refused by Twilio's own trial-account limits, see below |
| Simulated | `agent/notify/simulated.py` | ✅ | n/a — never touches the network by design |
| Twilio WhatsApp | `agent/notify/twilio_whatsapp.py` | ✅ mocked (`tests/agent/test_twilio_whatsapp_channel.py`, 16 tests) | 🔶 2026-09-01 — sender genuinely live (real send accepted and routed); a real Content Template is submitted and in WhatsApp review, which a cold send needs, see below |
| SMS / email | not implemented | — | — |

"Tested" means I assert the request shape (URL, body, form/JSON encoding)
and every response path (success, a clean API-level rejection, a network
failure) against `httpx.MockTransport` — no real network call happens in
the default test run, matching my existing policy for `RazorpayRail`
(`tests/agent/test_razorpay_rail_live.py` is opt-in only).

## Live verification, 2026-08-31

Run via `uv run python tools/verify_credentials.py` (never prints a secret
value):

- **Telegram — fully confirmed, including a real send.** `get_me()`
  returned the bot's identity; once I messaged the bot,
  `tools/telegram_get_chat_id.py` recovered a real `chat_id`, and I sent a
  real message through `/demo/trigger` — `status: "sent"`, a real
  Telegram `message_id` back as `external_ref`, checked against all 19
  bounds rules first. This is the actual send path, not the identity-only
  check.
- **Twilio — credentials confirmed; the real call was cleanly refused by
  Twilio itself.** I authenticated via a Twilio **API Key**
  (`TWILIO_API_KEY_SID`/`TWILIO_API_KEY_SECRET`), not the classic Account
  Auth Token, which I hadn't provided — this required a real fix, not a
  workaround: `TwilioVoiceChannel` now takes an `auth_username` parameter
  (the API Key SID goes in the HTTP Basic Auth *username*, its Secret in
  the password; `account_sid` still identifies the account in the URL path
  regardless of which auth scheme is used). I then attempted a real call
  to a real number and Twilio's API answered with a clean, specific
  rejection rather than a network/auth error:
  `"Invalid or disallowed parameters provided -- trial accounts have
  limited parameter access, upgrade your account to unlock full
  functionality."` Most likely cause: Twilio trial accounts can normally
  only call numbers verified in the Console (Phone Numbers -> Verified
  Caller IDs), or the inline `Twiml` parameter I use specifically is
  trial-restricted. `TwilioVoiceChannel.send()` handled this exactly as I
  designed it — a clean `status="failed"` result with the real reason in
  `detail`, not an exception, not a silent false success. Finding this
  live also caught a real gap in `/demo/trigger`'s response (it dropped
  `detail` entirely) and in the dashboard's own JS (it checked HTTP
  status, not the actual send status, so this exact failure would have
  shown as a false "sent" in the UI) — I fixed both, see `docs/DEMO_UI.md`.

**I tried a second, newly-generated API Key and it was rejected
outright** — `401`, Twilio error code
[70051](https://www.twilio.com/docs/errors/70051), `"Authorization Error:
actor doesn't have any assertions"`. This is a *different* failure mode
than the trial-account limit above: it means the key itself has no
permissions granted, before Twilio even gets to evaluate the call —
consistent with the key having been generated from a restricted-scope
section of the Console (e.g. a Voice/Video SDK access-token page) rather
than the general-purpose Account -> API keys & tokens page that issues
"Standard" keys with REST API access. I reverted `.env` to the
previously-working key (`SK4c10bb...`) rather than keep a key that fails
even the read-only `verify_credentials()` check. If I want a working
replacement key later, it needs to come from Twilio Console's **API keys &
tokens** page specifically, type **Standard**.

**A third Twilio credential change, 2026-08-31: I switched accounts
entirely.** I supplied a different Twilio account's classic Account SID +
Auth Token (not an API Key this time). It authenticates cleanly
(`GET .../Accounts/{sid}.json` → `200`, `status: active`) — but a check of
`GET .../Accounts/{sid}/IncomingPhoneNumbers.json` came back empty: this
account owns zero phone numbers, so there's nothing yet to set
`TWILIO_FROM_NUMBER` to. Not something to fix in code — I need a real
number claimed in the Twilio Console (Phone Numbers -> Buy a Number; trial
accounts usually get one free). I deliberately didn't purchase one
programmatically here even though the API supports it, since claiming a
number is a real account/billing action, not a read-only check.

**Update, 2026-09-01: the account was upgraded (Trial -> Full, real $20
balance confirmed live) and I bought a real number
(`+1 937 646-7656`, voice + SMS + MMS capable) once the account's Trust
Hub compliance profile cleared.** This unblocked things a trial account
structurally can't do: the Content API (needed for real WhatsApp message
templates), number search, and unrestricted outbound calls. I built
`TwilioWhatsAppChannel` (`agent/notify/twilio_whatsapp.py`) against
Twilio's real Messages API — a genuinely different endpoint shape from the
direct Meta integration (`agent/notify/whatsapp.py`), not a config flip on
the same channel.

**The Content Template, and one real rejection worth recording.** A cold
WhatsApp send needs a pre-approved template
(`TWILIO_WHATSAPP_CONTENT_SID`, defaulted in `agent/api/demo.py` since a
ContentSid is a resource id rather than a secret). The first submission
was rejected by Meta within seconds -- *"Variables can't be at the start
or end of the template"* -- because its body ended with the payment-link
variable. The fix is trivial once the reason is visible (put real text
after the link), but the reason is only visible by reading the approval
resource back: the create and submit calls both succeed, and the
rejection lands asynchronously afterward.

**Update, 2026-09-01: the WhatsApp sender registration cleared.** Getting
there hit two separate external blockers, neither in this codebase: (1)
the flow initially auto-selected a WhatsApp Business Account already
flagged "restricted" by Meta, tied to an earlier, abandoned direct-Meta-App
attempt on the same Facebook identity; and (2) after getting past that,
repeated verification-code requests tripped Meta's own rate limiter, which
doesn't publish a fixed cooldown window. Both resolved on their own with
time (not a code fix).

**Live-verified once it cleared**: a real `POST .../Messages.json` with
`From=whatsapp:+19376467656` was accepted and routed by Twilio (no
sender-registration error) — confirming the sender itself is genuinely
live. It came back `status: undelivered`, `error_code: 63016`
("WhatsApp Messaging Window Violation" — checked against Twilio's own
error docs, not guessed from the code alone): a plain `Body` text only
delivers within 24 hours of the recipient's own last message, or via a
pre-approved Content Template for a cold send — the same platform rule
every WhatsApp provider enforces, Twilio included, not a bug or an
account restriction. `TwilioWhatsAppChannel.send()` is real, tested,
and now provably reaches a genuinely registered sender; a real Content
Template (Content API, unlocked on the Full-tier account) is the
remaining piece needed for a cold outbound send — the actual use case
this project needs, since a debtor won't have messaged first.

**Update, 2026-09-02 — that platform rule is now a bounds rule.**
`WHATSAPP_SESSION_WINDOW` (`agent/bounds/rules.yaml`, rule 20 of 20)
refuses a free-form WhatsApp send when the debtor's last inbound message
is older than 24 hours, and requires the template path instead. It is
filed in the `stopping` register rather than `regulatory`: Meta's policy is
a vendor's terms of service, not law, and filing it beside RBI/TRAI/MSMED
would overstate it — a test asserts which register it lives in.

Error 63016 is why this rule is better sourced than most: it was not only
read in Meta's documentation, it was *hit*, by a real send from this
account. The rule models a constraint this project has actually been
refused by.

For a collections agent the distinction is load-bearing rather than
cosmetic. The conversational reply path and the cold-outreach path are
structurally different actions, and an agent that does not model the
difference silently fails to deliver at exactly the moment it believes it
is chasing. A message the platform drops is worse than one the gate
refuses — the gate's refusal is at least logged.

Adding the rule immediately failed nine existing tests, each marking a
place this codebase sent on WhatsApp without declaring which kind of send
it was: the demo trigger (a template), the conversational follow-up
(free-form, but always answering an inbound message and therefore inside
the window), and the adversarial channel-hopper (cold outreach, template
only). It also surfaced a live defect — `agent/api/app.py` selected
WhatsApp as the orchestrator's channel while telling the bounds gate
"telegram", so `TRAI_DND` checked the wrong channel's opt-out list and this
new rule could never fire on the one automated path able to send there.
See `docs/WHAT_BROKE.md` #21.

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
