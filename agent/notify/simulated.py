"""In-memory MessageChannel — the default for tests and for any channel a
debtor hasn't been wired to yet. Mirrors agent.rails.simulated.SimulatedRail:
never touches the network, records everything it was asked to send so a
test can assert on it directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.notify.protocol import MessageSendResult


@dataclass
class SimulatedChannel:
    channel_tag: str = "simulated"
    sent: list[dict] = field(default_factory=list)

    def send(self, *, to: str, text: str) -> MessageSendResult:
        self.sent.append({"to": to, "text": text})
        return MessageSendResult(
            channel=self.channel_tag,
            external_ref=f"sim-{len(self.sent)}",
            status="sent",
            detail={"to": to},
        )
