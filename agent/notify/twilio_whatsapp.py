"""Twilio WhatsApp channel -- DEVDOC_v6's "whatsapp" channel, via Twilio's
Messages API rather than a direct Meta Business Platform integration
(agent/notify/whatsapp.py). Deliberately a separate module: this one talks
to Twilio's Messages endpoint (POST .../Messages.json with whatsapp: To/
From prefixes), a genuinely different API shape from Meta's own Graph API
messages endpoint -- not a config flip on the existing channel.

Built specifically for the Sandbox path: Twilio's shared sandbox number
(conventionally whatsapp:+14155238886) sends and receives real WhatsApp
messages with **zero Meta business verification** -- the recipient only
has to text "join <sandbox-code>" to that number once, then Twilio can
message them freely for the sandbox's session lifetime. This is
demo/dev-only, not a production channel: a real, branded WhatsApp
business number still needs the same underlying Meta business
verification agent/notify/whatsapp.py's direct integration was built
for -- Twilio is a Meta Business Solution Provider on the back end, not a
way around that requirement. This module sidesteps it for exactly the
sandbox use case, nothing more.
"""

from __future__ import annotations

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
