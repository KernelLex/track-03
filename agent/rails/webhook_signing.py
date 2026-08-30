"""HMAC-SHA256 webhook signing/verification — the one code path both rails share. DEVDOC_v6 §9.2.

`SimulatedRail` signs with this exact scheme so the ingest verification path
can't tell a simulated webhook from a real one by its signature — which is
the point: it forces one code path to handle both, instead of a "trust the
simulator" shortcut that would never get exercised against a real signature.
"""

from __future__ import annotations

import hashlib
import hmac


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify(body: bytes, signature: str, secret: str) -> bool:
    expected = sign(body, secret)
    return hmac.compare_digest(expected, signature)
