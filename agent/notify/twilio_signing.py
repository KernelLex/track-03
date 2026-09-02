"""Verify that an inbound webhook really came from Twilio.

`/demo/whatsapp-webhook` is a public endpoint that makes the system answer a
debtor. Without this, anyone who found the URL could fabricate a reply and
drive the conversation -- the same class of risk `verify_and_ingest()` closes
for Razorpay and `secret_token` closes for Telegram, and it has to be closed
the same way: verify before doing anything at all with the body.

Twilio's scheme is not Meta's. Meta signs the raw body with HMAC-SHA256
(`agent/notify/whatsapp.py::verify_webhook_signature`); Twilio signs the
*request URL concatenated with its sorted form parameters* using HMAC-SHA1,
keyed by the account's auth token. Two providers, both called "WhatsApp" in
this project, with incompatible signing -- which is exactly the confusion
`docs/WHATSAPP.md` warns about, so it is spelled out here too.

**The proxy trap, handled deliberately rather than discovered later.** Twilio
computes its signature over the URL *it* requested -- always `https://` for a
public deployment. Behind Render's TLS terminator the app sees `http://`, so
`str(request.url)` reconstructs a different string and every signature fails.
`public_url_for()` rebuilds the URL Twilio actually used, preferring an
explicit `TRUECOMMIT_PUBLIC_BASE_URL` and falling back to the
`X-Forwarded-Proto` header. Getting this wrong fails closed (every request
rejected), which is the safe direction, but it fails *silently* in the sense
that the cause is not obvious from the symptom.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from urllib.parse import urlsplit, urlunsplit


def expected_signature(url: str, params: dict[str, str], auth_token: str) -> str:
    """Twilio's documented algorithm: append each POST parameter to the full
    URL, sorted by key, as `key + value` with no separators; HMAC-SHA1 that
    with the auth token; base64 the digest."""
    payload = url
    for key in sorted(params):
        payload += key + (params[key] or "")
    digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_twilio_signature(
    *, url: str, params: dict[str, str], signature: str | None, auth_token: str
) -> bool:
    """Constant-time compare, for the same reason
    `agent.rails.webhook_signing.verify` uses one: a byte-at-a-time
    comparison leaks the correct signature to anyone willing to time it.

    A missing header is a failure, not a skip. An endpoint that accepts an
    unsigned request whenever the header is absent is unauthenticated with
    extra steps -- this project already shipped one fail-open contact guard
    (`docs/WHAT_BROKE.md` #25) and is not shipping another.
    """
    if not signature or not auth_token:
        return False
    return hmac.compare_digest(expected_signature(url, params, auth_token), signature)


def public_url_for(*, request_url: str, forwarded_proto: str | None = None) -> str:
    """Rebuild the URL Twilio signed.

    Precedence, most explicit first:

    1. `TRUECOMMIT_PUBLIC_BASE_URL` -- set this when the deployment sits
       behind anything that rewrites host or scheme. It is the only option
       that cannot be spoofed by a request header.
    2. `X-Forwarded-Proto` -- what Render and most reverse proxies send.
    3. The URL as the app saw it.

    Only scheme and netloc are ever replaced; path and query come from the
    real request, so this cannot be used to point verification at some other
    route.
    """
    parts = urlsplit(request_url)
    base = os.environ.get("TRUECOMMIT_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base:
        base_parts = urlsplit(base if "//" in base else f"https://{base}")
        return urlunsplit((base_parts.scheme, base_parts.netloc, parts.path, parts.query, ""))
    if forwarded_proto:
        return urlunsplit((forwarded_proto.split(",")[0].strip(), parts.netloc, parts.path, parts.query, ""))
    return request_url
