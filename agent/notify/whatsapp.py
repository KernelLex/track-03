"""WhatsApp Business Cloud API channel (Meta Graph API) — DEVDOC_v6's
"whatsapp" channel (agent.bounds.context.ALL_CHANNELS). Built ahead of
having a real access token: every request shape here is exercised against
Meta's documented Cloud API contract via httpx.MockTransport
(tests/agent/test_notify_channels.py), the same way TwilioVoiceChannel was
built and proven before its own live credentials existed. Swapping in a
real WHATSAPP_ACCESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID is meant to be the only
step left — no code path here is a placeholder.

Two sends, not one, because WhatsApp has a real constraint the other
channels don't: a business can only send a free-form `send()` text message
inside the 24-hour window after the user last messaged in (Meta's
"customer service window"). Outside that window, only a pre-approved
*template* message can open a new conversation — hence `send_template()`,
with its URL button's dynamic part restricted to a *suffix* appended to a
base URL fixed at template-approval time (Meta doesn't allow arbitrary
dynamic URLs in a button, only a fixed prefix + one variable suffix).

Not raw REST out of a library preference — same reasoning as
agent/notify/twilio_voice.py: one real endpoint, httpx already a
dependency, no SDK needed for this surface.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field

import httpx

from agent.notify.protocol import ChannelUnavailable, MessageSendResult

GRAPH_API_BASE = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v25.0"


class WhatsAppChannel:
    channel_tag = "whatsapp"

    def __init__(
        self,
        phone_number_id: str,
        access_token: str,
        *,
        api_version: str = DEFAULT_API_VERSION,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ):
        """`transport` (e.g. httpx.MockTransport in tests) plugs into the
        client this constructor builds, so the Bearer auth header is still
        exercised for real -- unlike passing a fully pre-built `client`,
        which is used as-is and owns its own headers (same distinction
        agent.notify.twilio_voice.TwilioVoiceChannel makes)."""
        if not (phone_number_id and access_token):
            raise ValueError("WhatsAppChannel requires phone_number_id and access_token")
        self._phone_number_id = phone_number_id
        self._client = client or httpx.Client(
            timeout=timeout, headers={"Authorization": f"Bearer {access_token}"}, transport=transport
        )
        self._base = f"{GRAPH_API_BASE}/{api_version}/{phone_number_id}"

    def send(self, *, to: str, text: str) -> MessageSendResult:
        """Free-form session message — only deliverable within Meta's 24h
        customer-service window (the debtor must have messaged in recently).
        Outside that window Meta's API itself rejects this with a clean
        error (code 131047), which comes back here as status="failed", not
        an exception -- the API was reached and it said no. Use
        send_template() to open a conversation from cold."""
        return self._post_messages({
            "messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text},
        })

    def send_template(
        self,
        *,
        to: str,
        template_name: str,
        language_code: str = "en",
        body_params: list[str] | None = None,
        url_button_suffix: str | None = None,
    ) -> MessageSendResult:
        """Sends an approved template, the only way to message a debtor
        outside the 24h window. `url_button_suffix` fills the single dynamic
        variable on a template's URL button -- Meta only allows this to be a
        *suffix* appended to a base URL fixed when the template was
        approved (e.g. base `https://rzp.io/` + suffix `l/abc123`), never an
        arbitrary full URL. Omit it for a template with no URL button."""
        components: list[dict] = []
        if body_params:
            components.append({
                "type": "body", "parameters": [{"type": "text", "text": p} for p in body_params],
            })
        if url_button_suffix is not None:
            components.append({
                "type": "button", "sub_type": "url", "index": "0",
                "parameters": [{"type": "text", "text": url_button_suffix}],
            })
        template: dict = {"name": template_name, "language": {"code": language_code}}
        if components:
            template["components"] = components
        return self._post_messages({
            "messaging_product": "whatsapp", "to": to, "type": "template", "template": template,
        })

    def send_interactive_buttons(self, *, to: str, text: str, buttons: list[tuple[str, str]]) -> MessageSendResult:
        """A free-form message with up to 3 tappable reply buttons
        (id, title) -- still a session message, same 24h-window rule as
        send(). A tap comes back as a `button_reply` in the incoming
        webhook (see parse_incoming_message), routed to Path A instead of
        the LLM extractor: the debtor picked from a fixed, known set of
        options, so there's nothing free-text about it to extract."""
        if not 1 <= len(buttons) <= 3:
            raise ValueError("WhatsApp interactive messages support 1-3 buttons")
        return self._post_messages({
            "messaging_product": "whatsapp", "to": to, "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": text},
                "action": {"buttons": [
                    {"type": "reply", "reply": {"id": bid, "title": title}} for bid, title in buttons
                ]},
            },
        })

    def _post_messages(self, payload: dict) -> MessageSendResult:
        try:
            response = self._client.post(f"{self._base}/messages", json=payload)
        except httpx.HTTPError as exc:
            raise ChannelUnavailable("whatsapp", str(exc)) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise ChannelUnavailable("whatsapp", f"non-JSON response: {exc}") from exc

        if response.status_code >= 300 or "error" in body:
            error = body.get("error", {})
            return MessageSendResult(
                channel="whatsapp", external_ref=None, status="failed",
                detail={"status_code": response.status_code, "code": error.get("code"), "message": error.get("message")},
            )

        message_id = body["messages"][0]["id"]
        wa_id = body.get("contacts", [{}])[0].get("wa_id")
        return MessageSendResult(
            channel="whatsapp", external_ref=message_id, status="sent", detail={"wa_id": wa_id},
        )

    def verify_credentials(self) -> dict | None:
        """Fetches this phone number's own resource -- read-only, no
        message sent, no cost. Returns {"display_phone_number", "verified_name"}
        on success or None on any auth/network failure (tools/verify_credentials.py)."""
        try:
            response = self._client.get(
                f"{GRAPH_API_BASE}/{self._phone_number_id}",
                params={"fields": "display_phone_number,verified_name"},
            )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        body = response.json()
        if "error" in body:
            return None
        return {"display_phone_number": body.get("display_phone_number"), "verified_name": body.get("verified_name")}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "WhatsAppChannel":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def verify_webhook_signature(body: bytes, signature_header: str | None, app_secret: str) -> bool:
    """Meta signs every webhook POST with X-Hub-Signature-256: HMAC-SHA256
    of the raw request body, keyed by the app secret (not the access token —
    a different credential, from the Meta App's Basic Settings). Same
    constant-time-compare discipline as agent.rails.webhook_signing.verify
    uses for Razorpay's signature -- a non-constant-time comparison here
    would leak the correct signature one byte at a time via timing."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    provided = signature_header[len("sha256="):]
    return hmac.compare_digest(expected, provided)


def verify_webhook_challenge(*, mode: str | None, token: str | None, challenge: str | None, expected_token: str) -> str | None:
    """Meta's one-time GET handshake when a webhook URL is first registered
    in the App Dashboard: it sends hub.mode=subscribe, hub.verify_token
    (a value merchant-chosen at setup time, WHATSAPP_WEBHOOK_VERIFY_TOKEN),
    and hub.challenge (a random string). Echo hub.challenge back as the
    plain-text response body iff the token matches, else the setup screen
    shows the subscription as failed -- there is no other way to fail this
    handshake safely."""
    if mode == "subscribe" and token == expected_token and challenge:
        return challenge
    return None


@dataclass(frozen=True, slots=True)
class IncomingWhatsAppMessage:
    from_wa_id: str
    message_id: str
    type: str  # "text" | "interactive"
    text: str | None = None
    button_id: str | None = None
    button_title: str | None = None

    @property
    def is_structured_reply(self) -> bool:
        """True for a button tap -- routes to Path A (diagnose_from_failure_code
        or an equivalent fixed mapping), never the LLM: the debtor chose from
        a known, finite set of options, so there's no free text to extract
        and no reason to spend a Claude call on it (agent/spend.py's budget
        is better spent on genuinely unstructured replies)."""
        return self.type == "interactive"


def parse_incoming_messages(payload: dict) -> list[IncomingWhatsAppMessage]:
    """Extracts every real message from a Meta webhook POST body. Meta
    batches multiple `entry`/`changes` per delivery and also delivers
    delivery/read *status* updates through the same webhook (a `statuses`
    key instead of `messages` in `value`) -- those are silently skipped
    here, not an error, since this channel has nothing to diagnose from a
    read receipt. Malformed/unexpected shapes are skipped per-message
    rather than raising, so one odd payload can't 500 the whole delivery
    (mirrors the webhook endpoint's existing "never crash on a redelivery
    or a shape we don't recognize" discipline)."""
    out: list[IncomingWhatsAppMessage] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                parsed = _parse_one(msg)
                if parsed is not None:
                    out.append(parsed)
    return out


def _parse_one(msg: dict) -> IncomingWhatsAppMessage | None:
    from_wa_id = msg.get("from")
    message_id = msg.get("id")
    msg_type = msg.get("type")
    if not (from_wa_id and message_id and msg_type):
        return None

    if msg_type == "text":
        body = msg.get("text", {}).get("body")
        if body is None:
            return None
        return IncomingWhatsAppMessage(from_wa_id=from_wa_id, message_id=message_id, type="text", text=body)

    if msg_type == "interactive":
        interactive = msg.get("interactive", {})
        if interactive.get("type") == "button_reply":
            reply = interactive.get("button_reply", {})
            return IncomingWhatsAppMessage(
                from_wa_id=from_wa_id, message_id=message_id, type="interactive",
                button_id=reply.get("id"), button_title=reply.get("title"),
            )
        return None

    return None  # image/audio/location/etc. -- out of scope, not an error
