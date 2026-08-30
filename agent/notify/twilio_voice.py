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
        auth_username: str | None = None,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 15.0,
    ):
        """`auth_token` is the HTTP Basic Auth *password* — either the
        account's classic Auth Token (leave `auth_username` unset, it
        defaults to `account_sid`), or a Twilio API Key's Secret (pass the
        matching API Key SID, `SKxxx`, as `auth_username`). `account_sid`
        always identifies the account in the URL path either way — Twilio's
        API Key scheme authenticates *as* an API key but still acts *on* a
        specific account, so the two are independent, not alternatives.

        `transport` (e.g. `httpx.MockTransport` in tests) plugs into the
        client this constructor builds, so auth_username/auth_token are
        still exercised for real — unlike passing a fully pre-built
        `client`, which is used as-is and owns its own auth setup."""
        if not (account_sid and auth_token and from_number):
            raise ValueError("TwilioVoiceChannel requires account_sid, auth_token, and from_number")
        self._sid = account_sid
        self._from = from_number
        username = auth_username or account_sid
        self._client = client or httpx.Client(timeout=timeout, auth=(username, auth_token), transport=transport)
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

    def verify_credentials(self) -> dict | None:
        """Fetches the account resource -- read-only, no call placed, no
        cost. Returns the account's {friendly_name, status} on success or
        None on any auth/network failure, for a quick "do these credentials
        actually work" check (tools/verify_credentials.py) without the
        side effect (and cost) of send()."""
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

    def __enter__(self) -> "TwilioVoiceChannel":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
