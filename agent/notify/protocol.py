"""The MessageChannel Protocol — one interface, several implementations.

Deliberately separate from agent.rails.protocol.Rail: a Rail creates and
mutates Razorpay payment objects (a mandate, a payment link); a
MessageChannel only ever sends a human-readable message to a debtor. They
share no methods and are never satisfied by the same object, so this is a
distinct, smaller protocol rather than an extra method bolted onto Rail.

Every action that reaches a channel has already cleared check_bounds() —
this module has no opinion on whether a message should be sent, only on how
to actually deliver one once ACT has decided to.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel


class ChannelUnavailable(Exception):
    """Raised by a real channel implementation when the send could not be
    attempted or confirmed at all (network error, missing credentials,
    malformed recipient) — as opposed to a clean "failed" MessageSendResult,
    which means the channel's own API was reached and it said no. Mirrors
    agent.rails.types.RailUnavailable's split between "we don't know" and
    "we know, and it's a no." Never silently downgraded to a fake success."""

    def __init__(self, channel: str, detail: str = ""):
        self.channel = channel
        self.detail = detail
        super().__init__(f"channel unavailable: {channel}" + (f" — {detail}" if detail else ""))


class MessageSendResult(BaseModel):
    channel: str
    external_ref: str | None
    status: Literal["sent", "failed"]
    detail: dict = {}


@runtime_checkable
class MessageChannel(Protocol):
    channel_tag: str

    def send(self, *, to: str, text: str) -> MessageSendResult: ...
