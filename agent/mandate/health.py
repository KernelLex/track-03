"""Mandate health detectors — family A, preventive. DEVDOC_v6 §12.3.

Pure functions over object shape, same as §5.2 promises: no rail call needed
to run any of these, which is why the whole mandate-health story is
demonstrable without live mandate rails.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal

from agent.mandate.instrument import AFA_FREE_CEILING_PAISE

MandateStatus = Literal[
    "created", "pending_afa", "active", "health_defect", "repairing",
    "debit_scheduled", "notified_24h", "paused", "revoked", "expired",
]


class MandateDefect(str, Enum):
    HEADROOM_BREACH = "HEADROOM_BREACH"
    EXPIRY_BEFORE_DEBIT = "EXPIRY_BEFORE_DEBIT"
    AFA_THRESHOLD_BREACH = "AFA_THRESHOLD_BREACH"
    REPEAT_NSF = "REPEAT_NSF"
    SILENT_REVOCATION = "SILENT_REVOCATION"
    RAIL_DEGRADED = "RAIL_DEGRADED"


REPAIR_FOR_DEFECT: dict[MandateDefect, str] = {
    MandateDefect.HEADROOM_BREACH: "modify_mandate (AFA) or split into multiple debits",
    MandateDefect.EXPIRY_BEFORE_DEBIT: "re-register ahead of the cycle",
    MandateDefect.AFA_THRESHOLD_BREACH: "attach AFA to the pre-debit notice",
    MandateDefect.REPEAT_NSF: "re-time the debit; nudge in the notice",
    MandateDefect.SILENT_REVOCATION: "reach out before the missed cycle",
    MandateDefect.RAIL_DEGRADED: "route to an alternate registered rail",
}

REPEAT_NSF_THRESHOLD = 2
DEFAULT_ISSUER_FAILURE_RATE_THRESHOLD = 0.15
"""Not given a specific value in DEVDOC_v6 §12.3 beyond "elevated" — a starting
default, same spirit as the Auditor's 10% sampling default (§11.7)."""


@dataclass(frozen=True, slots=True)
class MandateSnapshot:
    max_amount_paise: int
    end_at: datetime
    status: MandateStatus
    afa_scheduled: bool = False
    consecutive_nsf: int = 0
    issuer_failure_rate: float | None = None
    """None when no data exists yet — treated as "not elevated", never as 0.0,
    so a mandate with no observations doesn't wrongly clear this check."""


@dataclass(frozen=True, slots=True)
class HealthCheckInput:
    mandate: MandateSnapshot
    upcoming_debit_paise: int
    next_debit_date: datetime
    cycle_was_attempted: bool
    """False only when a scheduled cycle came and went with no debit attempt at
    all — the SILENT_REVOCATION signal. True for a cycle that was attempted and
    failed (that's REPEAT_NSF's job, not this one)."""
    issuer_failure_rate_threshold: float = DEFAULT_ISSUER_FAILURE_RATE_THRESHOLD


@dataclass(frozen=True, slots=True)
class DetectedDefect:
    defect: MandateDefect
    repair: str
    detail: str


def check_mandate_health(inp: HealthCheckInput) -> list[DetectedDefect]:
    m = inp.mandate
    defects: list[DetectedDefect] = []

    if m.max_amount_paise < inp.upcoming_debit_paise:
        defects.append(DetectedDefect(
            MandateDefect.HEADROOM_BREACH, REPAIR_FOR_DEFECT[MandateDefect.HEADROOM_BREACH],
            f"max_amount_paise={m.max_amount_paise} < upcoming_debit_paise={inp.upcoming_debit_paise}",
        ))

    if m.end_at < inp.next_debit_date:
        defects.append(DetectedDefect(
            MandateDefect.EXPIRY_BEFORE_DEBIT, REPAIR_FOR_DEFECT[MandateDefect.EXPIRY_BEFORE_DEBIT],
            f"end_at={m.end_at.isoformat()} < next_debit_date={inp.next_debit_date.isoformat()}",
        ))

    if inp.upcoming_debit_paise > AFA_FREE_CEILING_PAISE and not m.afa_scheduled:
        defects.append(DetectedDefect(
            MandateDefect.AFA_THRESHOLD_BREACH, REPAIR_FOR_DEFECT[MandateDefect.AFA_THRESHOLD_BREACH],
            f"upcoming_debit_paise={inp.upcoming_debit_paise} > {AFA_FREE_CEILING_PAISE} with no AFA scheduled",
        ))

    if m.consecutive_nsf >= REPEAT_NSF_THRESHOLD:
        defects.append(DetectedDefect(
            MandateDefect.REPEAT_NSF, REPAIR_FOR_DEFECT[MandateDefect.REPEAT_NSF],
            f"consecutive_nsf={m.consecutive_nsf} >= {REPEAT_NSF_THRESHOLD}",
        ))

    if m.status in ("revoked", "paused") and not inp.cycle_was_attempted:
        defects.append(DetectedDefect(
            MandateDefect.SILENT_REVOCATION, REPAIR_FOR_DEFECT[MandateDefect.SILENT_REVOCATION],
            f"status={m.status!r} with no attempted cycle since",
        ))

    if m.issuer_failure_rate is not None and m.issuer_failure_rate > inp.issuer_failure_rate_threshold:
        defects.append(DetectedDefect(
            MandateDefect.RAIL_DEGRADED, REPAIR_FOR_DEFECT[MandateDefect.RAIL_DEGRADED],
            f"issuer_failure_rate={m.issuer_failure_rate} > threshold={inp.issuer_failure_rate_threshold}",
        ))

    return defects
