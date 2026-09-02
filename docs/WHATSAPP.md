# WhatsApp channel

I built this ahead of having a real WhatsApp Business account — code-complete
and tested (`tests/agent/test_whatsapp_channel.py`,
`tests/agent/test_whatsapp_webhook_routes.py`, 37 tests) against Meta's
documented Cloud API contract via `httpx.MockTransport`, the same way I
built and proved `TwilioVoiceChannel` before its own live credentials
existed (see `docs/CHANNELS.md`). The only thing left to go live is filling
in three real values in `.env` — nothing here is a placeholder that needs
rewriting later.

## Why Meta directly, not Twilio

Twilio offers a WhatsApp API too, but it's a reseller layer over the same
underlying Meta Graph API — going direct is the same channel with one
fewer hop, one fewer vendor relationship, and one fewer place a message
could fail. I made this decision deliberately, recorded in my 2026-09-01
handoff notes, not as a default I fell into.

## What I've built

- **`agent/notify/whatsapp.py`** — `WhatsAppChannel`, implementing the same
  `MessageChannel` protocol as `TelegramChannel`/`TwilioVoiceChannel`
  (`channel_tag = "whatsapp"`, `send(to, text)`), plus three real
  constraints Meta's API has that the others don't:
  - **`send()`** — free-form text, only deliverable inside Meta's 24-hour
    customer-service window (the debtor must have messaged in recently).
    Outside that window Meta's API rejects it cleanly (error code 131047),
    which comes back as `status="failed"`, not an exception.
  - **`send_template()`** — the only way to message a debtor from cold.
    Needs a template pre-approved in Meta's Business Manager. Its dynamic
    URL button is intentionally restricted to a **suffix** appended to a
    base URL fixed at template-approval time (e.g. base `https://rzp.io/` +
    suffix `l/abc123`) — Meta doesn't allow an arbitrary full URL in a
    button, only that one variable slot.
  - **`send_interactive_buttons()`** — up to 3 tappable reply buttons, for
    the in-window follow-up case (e.g. "I already paid" / "I dispute this"
    / "I need more time"). A tap comes back as a `button_reply` in the
    inbound webhook.
  - **`verify_credentials()`** — read-only phone-number identity check, no
    message sent, no cost (mirrors the other channels' credential checks).
  - **`verify_webhook_signature()`** — Meta's `X-Hub-Signature-256`
    (HMAC-SHA256 of the raw body, keyed by the **App Secret** — a different
    credential from the access token used to send). Same constant-time
    comparison discipline I use in `agent/rails/webhook_signing.py` for
    Razorpay.
  - **`verify_webhook_challenge()`** — Meta's one-time GET handshake when a
    webhook URL is first registered in the App Dashboard.
  - **`parse_incoming_messages()`** — extracts real messages from Meta's
    webhook body shape, distinguishing a free-text reply from a structured
    button tap (`IncomingWhatsAppMessage.is_structured_reply`), and quietly
    skips delivery/read receipt callbacks and unsupported message types
    (image/audio/location) rather than crashing on them.

- **`agent/api/app.py`** — two new routes, which I registered *before* the
  generic `POST /webhooks/{source}` route (a real bug I caught and fixed
  while building this: Starlette matches path routes in registration
  order, so a static `/webhooks/whatsapp` registered after the `{source}`
  wildcard would have been silently swallowed by the Razorpay handler
  instead of ever reaching Meta's own verification —
  `tests/agent/test_whatsapp_webhook_routes.py::test_route_is_not_swallowed_by_the_generic_source_wildcard`
  guards this specifically):
  - `GET /webhooks/whatsapp` — the verification handshake.
  - `POST /webhooks/whatsapp` — real inbound messages. Verifies the
    signature, parses each message, and diagnoses it immediately: a button
    tap goes through `WHATSAPP_BUTTON_DIAGNOSIS` (Path A, no model — the
    debtor picked from a known, finite set of options); free text goes
    through the real `extract_from_reply()` (Path B, same function I
    already live-verified for Telegram replies in `docs/LLM_EXTRACTION.md`).
    I dedupe against Meta's own redelivery using the same `EventStore`
    every Razorpay webhook dedupes through, keyed by each message's own
    `wamid` — without this, a retried delivery would double-bill a real
    Claude call for the same reply.
  - `lifespan()`'s channel selection now prefers `WhatsAppChannel` over
    `TelegramChannel` automatically the moment `WHATSAPP_PHONE_NUMBER_ID`
    and `WHATSAPP_ACCESS_TOKEN` are both set — Telegram was always a free
    stand-in for a channel a debtor can't be cold-messaged on, not the
    intended production channel.

## What I've deliberately not wired yet

A parsed, diagnosed inbound WhatsApp reply is **not** pushed through
`run_pipeline()` into DECIDE/BOUNDS/ACT the way a `payment.failed` webhook
is. The payment webhook path can derive a `debtor_id`/`invoice_id` from the
payment itself; a WhatsApp message only carries a `wa_id` (a phone number),
and I haven't built a merchant AR system to resolve that to a real invoice.
Wiring an inbound reply into the full pipeline needs that lookup first —
the same honestly-stated limitation I already document for
`debtor_id`/`invoice_id` derivation in `docs/ORCHESTRATION.md`, not a new
one.

`send_template()` and `send_interactive_buttons()` reference template
names and button ids (`payment_reminder`, `btn_already_paid`,
`btn_dispute`, `btn_need_time`) that don't exist yet in any real Meta
Business Manager — I still need to create and get those approved there
before a live send can use them. The code that calls them is real and
tested; the templates themselves are a Meta-console setup step, not a
code gap.

## Update, 2026-09-02 — a template is approved and a real send was delivered

**Read the previous paragraph carefully before assuming this supersedes it.
It does not.** The approved template lives on **Twilio**, not on the Meta
Cloud API path that `agent/notify/whatsapp.py` implements. Those are two
different integrations that both say "WhatsApp", and conflating them is the
easiest mistake to make in this file.

What is now true:

```
name:              truecommit_invoice_reminder_v2
sid:               HX7fab710a0f32ab9e8a1be21250bf98a3
status:            approved
category:          UTILITY
rejection_reason:  (none)
```

Body, with its four variables:

> Hi, this is TrueCommit on behalf of Acme Textiles. Invoice `{{1}}` for Rs
> `{{2}}` is now `{{3}}` days overdue. You can pay here: `{{4}}` — reply to
> this message if anything about the invoice looks wrong.

A real cold outbound was sent against it to a real handset —
`MM8227e461795d36d03ca12dd3e2553ade` — and **polled to a terminal status of
`delivered`**. That distinction matters: Twilio's create call returns
`queued`, and a document that reported `queued` as success would be claiming
delivery it had not observed. The status above was read back from
`GET /Messages/{sid}.json` after the fact.

This closes the "cold outbound needs an approved template" blocker: a debtor
who has never messaged first can now be contacted on WhatsApp. It is also
what makes `WHATSAPP_SESSION_WINDOW` (bounds rule 20) load-bearing rather
than theoretical — outside the 24-hour window, only an approved template may
be sent, and that rule is now enforcing a constraint the system can actually
run into.

**What is still not done**, so this section is not read as more than it is:

* `agent/notify/whatsapp.py` — the direct Meta Cloud API module — **still
  has not made a real call**. It stays tested against `httpx.MockTransport`
  only, and `WHATSAPP_ACCESS_TOKEN` / `WHATSAPP_APP_SECRET` are still unset.
  Everything above went through Twilio.
* **No inbound reply is wired into the pipeline.** A WhatsApp reply carries a
  `wa_id` and nothing else; resolving that to a `debtor_id` and an
  `invoice_id` needs the merchant AR lookup described below and in
  `docs/ORCHESTRATION.md`.
* The interactive-button templates are still uncreated. Only the plain-text
  reminder is approved.

## Fields needed to go live

From my 2026-09-01 handoff notes: `phone_number_id=1306946662503182`,
`WABA_ID=2439606209900856`, Graph API `v25.0` (already the default), a
verified recipient `+91 96115 50053`. Still needed in `.env`:
`WHATSAPP_ACCESS_TOKEN` (Meta App Dashboard → WhatsApp → API Setup),
`WHATSAPP_APP_SECRET` (App Dashboard → Settings → Basic — a different
credential from the access token), and a self-chosen
`WHATSAPP_WEBHOOK_VERIFY_TOKEN` (entered again in the webhook setup screen
when registering this server's `/webhooks/whatsapp` URL — needs a publicly
reachable URL; the Razorpay webhook receiver now runs on a permanent
Render deployment rather than a tunnel, see `docs/LIMITATIONS.md`, and
this route would use the same one).
