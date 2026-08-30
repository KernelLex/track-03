"""Twilio Voice channel — real phone calls, the only per-use-cost channel in
this project (DEVDOC_v6's "ivr" channel). Deliberately raw REST over httpx
rather than the `twilio` SDK: the call surface needed here is one endpoint
(POST .../Calls.json with inline TwiML), and the project already depends on
httpx for everything else — see docs/CHANNELS.md for why this was judged
not worth a second HTTP-client dependency.

Speaks `text` via Twilio's <Say> text-to-speech rather than requiring a
hosted TwiML URL, so a call can be placed with nothing but account
credentials and a phone number — no webhook endpoint needed for the voice
script itself (Twilio still needs to reach this account's status-callback
URL if one is configured, but none is required for a basic call).
"""

from __future__ import annotations

from xml.sax.saxutils import escape as _xml_escape

import httpx

from agent.notify.protocol import ChannelUnavailable, MessageSendResult

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


class TwilioVoiceChannel:
    channel_tag = "ivr"

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 15.0,
    ):
        if not (account_sid and auth_token and from_number):
            raise ValueError("TwilioVoiceChannel requires account_sid, auth_token, and from_number")
        self._sid = account_sid
        self._from = from_number
        self._client = client or httpx.Client(timeout=timeout, auth=(account_sid, auth_token))
        self._base = f"{TWILIO_API_BASE}/Accounts/{account_sid}"

    def send(self, *, to: str, text: str) -> MessageSendResult:
        """`to` is an E.164 phone number. `text` is read aloud via <Say> —
        escaped as XML content, since it can originate from a template that
        interpolates debtor-supplied entities (Law 8: never trust structure
        in text that reaches an outbound channel either)."""
        twiml = f"<Response><Say>{_xml_escape(text)}</Say></Response>"
        try:
            response = self._client.post(
                f"{self._base}/Calls.json",
                data={"To": to, "From": self._from, "Twiml": twiml},
            )
        except httpx.HTTPError as exc:
            raise ChannelUnavailable("ivr", str(exc)) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise ChannelUnavailable("ivr", f"non-JSON response: {exc}") from exc

        if response.status_code >= 300:
            return MessageSendResult(
                channel="ivr",
                external_ref=None,
                status="failed",
                detail={"status_code": response.status_code, "message": body.get("message")},
            )

        return MessageSendResult(
            channel="ivr",
            external_ref=body.get("sid"),
            status="sent",
            detail={"status": body.get("status")},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TwilioVoiceChannel":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
