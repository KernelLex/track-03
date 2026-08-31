"""The full subscription-side story, connected end to end: detect a
mandate defect before it fails a debit, repair it for real against a
Rail, then present the now-healthy debit for capture. DEVDOC_v6 §12.3
(health), §12.4 (pre-debit notice), §12.5 (lifecycle).

health.py's detectors and the Rail protocol's modify_mandate/present_debit
already existed — this module is the orchestration DEVDOC_v6 always
assumed would connect them, the same relationship agent/orchestrate.py has
to DIAGNOSE/DECIDE/BOUNDS/ACT: no new primitive, just the wiring between
real primitives that already existed in isolation.

Every step calls the same agent.rails.protocol.Rail methods every other
caller uses — this is not a second, parallel mandate implementation.
`RazorpayRail.modify_mandate()`/`.present_debit()` both raise
`RailUnavailable` today (see that module's own docstring for why —
Razorpay's only recurring primitive on this account is a fixed-schedule
Subscription, not the on-demand mandate DEVDOC_v6 assumes); this module
lets that exception propagate rather than silently downgrading to a fake
success, so a caller always knows exactly which rail actually ran the
repair. Fully real and tested against `SimulatedRail`, which implements
both for real.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from agent.mandate.health import DetectedDefect, HealthCheckInput, MandateDefect, MandateSnapshot, check_mandate_health
from agent.rails.protocol import Rail
from agent.rails.types import DebitResult, Mandate, MandateDelta

AUTO_REPAIRABLE: frozenset[MandateDefect] = frozenset({
    MandateDefect.HEADROOM_BREACH, MandateDefect.EXPIRY_BEFORE_DEBIT,
})
"""The two defects whose §12.3 repair column names a mandate-parameter fix
(modify_mandate) rather than a human/contact action. REPEAT_NSF
("re-time the debit; nudge in the notice"), SILENT_REVOCATION ("reach out
before the missed cycle"), and RAIL_DEGRADED ("route to an alternate
registered rail") all name a repair that isn't a mandate-parameter change
-- deliberately excluded here rather than guessed at."""


class UnrepairableDefect(Exception):
    """A detected defect this module doesn't auto-repair (not in
    AUTO_REPAIRABLE) -- raised rather than silently presenting a debit
    against a mandate still carrying an unaddressed defect. Carries every
    detected defect, not just the unrepairable one, so a caller sees the
    whole picture."""

    def __init__(self, defects: list[DetectedDefect]):
        self.defects = defects
        unrepairable = [d for d in defects if d.defect not in AUTO_REPAIRABLE]
        super().__init__(
            "cannot auto-repair: " + "; ".join(f"{d.defect.value} ({d.repair})" for d in unrepairable)
        )


@dataclass(frozen=True, slots=True)
class RepairResult:
    mandate_id: str
    defects_detected: list[DetectedDefect]
    defects_repaired: list[MandateDefect]
    mandate_after_repair: Mandate


def detect_and_repair(
    *,
    rail: Rail,
    mandate_id: str,
    mandate: MandateSnapshot,
    upcoming_debit_paise: int,
    next_debit_date: datetime,
    cycle_was_attempted: bool = True,
) -> RepairResult:
    """Runs the real check_mandate_health() detector, then repairs every
    auto-repairable defect via a real rail.modify_mandate() call — one
    call per defect, since HEADROOM_BREACH and EXPIRY_BEFORE_DEBIT can
    both be present and each needs its own MandateDelta. Raises
    UnrepairableDefect if any *other* defect remains — callers must not
    proceed to present a debit against a mandate this function couldn't
    actually fix."""
    defects = check_mandate_health(HealthCheckInput(
        mandate=mandate, upcoming_debit_paise=upcoming_debit_paise, next_debit_date=next_debit_date,
        cycle_was_attempted=cycle_was_attempted,
    ))

    unrepairable = [d for d in defects if d.defect not in AUTO_REPAIRABLE]
    repaired: list[MandateDefect] = []
    current: Mandate | None = None

    for d in defects:
        if d.defect == MandateDefect.HEADROOM_BREACH:
            current = rail.modify_mandate(mandate_id, MandateDelta(max_amount_paise=upcoming_debit_paise))
            repaired.append(d.defect)
        elif d.defect == MandateDefect.EXPIRY_BEFORE_DEBIT:
            current = rail.modify_mandate(mandate_id, MandateDelta(end_at=next_debit_date.isoformat()))
            repaired.append(d.defect)

    if unrepairable:
        raise UnrepairableDefect(defects)

    if current is None:
        current = Mandate.model_validate(rail.fetch("mandates", mandate_id))

    return RepairResult(
        mandate_id=mandate_id, defects_detected=defects, defects_repaired=repaired, mandate_after_repair=current,
    )


def notify_and_present_debit(
    *,
    rail: Rail,
    mandate_id: str,
    amount_paise: int,
    debit_datetime: str,
    reason: str = "scheduled subscription debit",
) -> DebitResult:
    """Sends the mandatory pre-debit notice (§12.4), then presents the
    debit (§12.5). The two calls are separate on purpose, not merged into
    one — a real ≥24h gap has to elapse between them (RBI_EMANDATE_
    PREDEBIT_24H, and SimulatedRail enforces the identical gate
    independently of BOUNDS, per its own module docstring), so a caller
    schedules `present_debit` for real, ≥24h later, not synchronously
    after `notify_predebit`.

    `notify_predebit` isn't on the formal Rail protocol (agent.rails.
    protocol.Rail) — it's how SimulatedRail models the mandatory notice's
    own timing bookkeeping. A real deployment sends the actual notice text
    through a MessageChannel (agent.notify.*) and separately tracks when
    it was sent; that message-plus-tracking wiring is a real next step,
    not built here — this function calls `notify_predebit` when the rail
    exposes it (SimulatedRail) and raises plainly when it doesn't, rather
    than silently skipping the mandatory notice."""
    notify = getattr(rail, "notify_predebit", None)
    if notify is None:
        raise NotImplementedError(
            f"rail_tag={getattr(rail, 'rail_tag', '?')!r} has no notify_predebit() -- the mandatory "
            "pre-debit notice (§12.4) has no rail-level equivalent on this Rail; a real deployment "
            "sends it through a MessageChannel and tracks the 24h window itself, not built here"
        )
    notify(mandate_id, amount_paise, debit_datetime, reason)
    return rail.present_debit(mandate_id, amount_paise)
