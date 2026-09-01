"""Twilio WhatsApp channel -- DEVDOC_v6's "whatsapp" channel, via Twilio's
Messages API rather than a direct Meta Business Platform integration
(agent/notify/whatsapp.py). Deliberately a separate module: this one talks
to Twilio's Messages endpoint (POST .../Messages.json with whatsapp: To/
From prefixes), a genuinely different API shape from Meta's own Graph API
messages endpoint -- not a config flip on the existing channel.

Real, verified WhatsApp sender as of 2026-09-01 (docs/CHANNELS.md), not
the classic shared sandbox this module started against -- registering a
real number as a sender through Twilio's guided flow is lighter than
Meta's own direct business verification, but doesn't remove the
underlying Meta requirement (Twilio is a Meta Business Solution Provider,
not a way around it): free-form `send()` still only works inside a 24h
customer-service window (WhatsApp's own platform rule, error 63016
outside it, checked against Twilio's own error docs, not guessed), and
`send_template()` is the only way to message a debtor from cold, the same
two-tier shape agent/notify/whatsapp.py's direct integration already has.
"""

from __future__ import annotations

import json

import httpx

from agent.notify.protocol import ChannelUnavailable, MessageSendResult

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


def _as_whatsapp(number: str) -> str:
    return number if number.startswith("whatsapp:") else f"whatsapp:{number}"


class TwilioWhatsAppChannel:
    channel_tag = "whatsapp"

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        *,
        auth_username: str | None = None,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 15.0,
    ):
        """`from_number` may be given as a plain E.164 number or already
        `whatsapp:`-prefixed -- normalized here either way, so a caller
        passing the sandbox's own `whatsapp:+14155238886` or a bare
        `+14155238886` both work. `auth_token`/`auth_username` follow the
        identical classic-Auth-Token-vs-API-Key split
        `agent.notify.twilio_voice.TwilioVoiceChannel` already uses --
        same account, same credentials, different endpoint."""
        if not (account_sid and auth_token and from_number):
            raise ValueError("TwilioWhatsAppChannel requires account_sid, auth_token, and from_number")
        self._sid = account_sid
        self._from = _as_whatsapp(from_number)
        username = auth_username or account_sid
        self._client = client or httpx.Client(timeout=timeout, auth=(username, auth_token), transport=transport)
        self._base = f"{TWILIO_API_BASE}/Accounts/{account_sid}"

    def send(self, *, to: str, text: str) -> MessageSendResult:
        """`to` is a plain E.164 phone number (e.g. "+919611550053") or
        already `whatsapp:`-prefixed -- normalized the same way `from_number`
        is. Raises ChannelUnavailable only when the call itself couldn't
        complete; a clean Twilio-side rejection (most commonly: this
        recipient never sent the sandbox's "join <code>" message, or the
        sandbox session with them expired) comes back as a clean
        status="failed" result, since that's Twilio's API answering, not an
        unknown outcome."""
        try:
            response = self._client.post(
                f"{self._base}/Messages.json",
                data={"To": _as_whatsapp(to), "From": self._from, "Body": text},
            )
        except httpx.HTTPError as exc:
            raise ChannelUnavailable("whatsapp", str(exc)) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise ChannelUnavailable("whatsapp", f"non-JSON response: {exc}") from exc

        if response.status_code >= 300:
            return MessageSendResult(
                channel="whatsapp", external_ref=None, status="failed",
                detail={"status_code": response.status_code, "message": body.get("message"), "code": body.get("code")},
            )

        return MessageSendResult(
            channel="whatsapp", external_ref=body.get("sid"), status="sent",
            detail={"status": body.get("status")},
        )

    def send_template(self, *, to: str, content_sid: str, content_variables: dict[str, str]) -> MessageSendResult:
        """Send a pre-approved WhatsApp Content Template -- the only way to
        message a debtor from cold (outside the 24h window `send()` needs).
        `content_sid` comes from Twilio's Content API (`HX...`); Twilio
        substitutes `content_variables` into the template's `{{1}}`,
        `{{2}}`, ... placeholders server-side. Live-verified: a raw
        `whatsapp:+E164` `From` works here exactly like it does for
        `send()` -- no Messaging Service SID needed, despite some
        documentation implying otherwise. Same failure handling as
        `send()`: a clean Twilio-side rejection (most commonly, right after
        submitting a template: still pending WhatsApp approval) comes back
        as status="failed", not an exception."""
        try:
            response = self._client.post(
                f"{self._base}/Messages.json",
                data={
                    "To": _as_whatsapp(to), "From": self._from,
                    "ContentSid": content_sid, "ContentVariables": json.dumps(content_variables),
                },
            )
        except httpx.HTTPError as exc:
            raise ChannelUnavailable("whatsapp", str(exc)) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise ChannelUnavailable("whatsapp", f"non-JSON response: {exc}") from exc

        if response.status_code >= 300:
            return MessageSendResult(
                channel="whatsapp", external_ref=None, status="failed",
                detail={"status_code": response.status_code, "message": body.get("message"), "code": body.get("code")},
            )

        return MessageSendResult(
            channel="whatsapp", external_ref=body.get("sid"), status="sent",
            detail={"status": body.get("status")},
        )

    def list_messages(self, *, to: str, from_: str, limit: int = 5) -> list[dict]:
        """Recent messages between `to` (usually this channel's own number)
        and `from_` (the other party), newest first -- Twilio's own default
        ordering, live-verified. Exists so a real inbound reply can be found
        by polling this account's own message history, the same shape
        TelegramChannel.get_updates() serves for Telegram, without needing
        a live webhook + Twilio Console configuration for a demo/dev
        surface. Raises ChannelUnavailable only when the call itself
        couldn't complete or Twilio's API rejected it outright."""
        try:
            response = self._client.get(
                f"{self._base}/Messages.json",
                params={"To": _as_whatsapp(to), "From": _as_whatsapp(from_), "PageSize": limit},
            )
        except httpx.HTTPError as exc:
            raise ChannelUnavailable("whatsapp", str(exc)) from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise ChannelUnavailable("whatsapp", f"non-JSON response: {exc}") from exc
        if response.status_code >= 300:
            raise ChannelUnavailable("whatsapp", f"list messages failed: {body}")
        return body.get("messages", [])

    def verify_credentials(self) -> dict | None:
        """Fetches the account resource -- read-only, no message sent, no
        cost. Mirrors TwilioVoiceChannel.verify_credentials() exactly; the
        same credentials authenticate both channels since they're the same
        Twilio account, just a different API surface."""
        try:
            response = self._client.get(f"{self._base}.json")
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        body = response.json()
        return {"friendly_name": body.get("friendly_name"), "status": body.get("status")}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TwilioWhatsAppChannel":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
