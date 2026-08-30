"""Arm A: fixed standard dunning schedule, no model. DEVDOC_v6 §17.4 — the
control arm.

This is the one arm buildable without an LLM or a persona-simulation engine:
a deterministic function of days-since-due, nothing else. Running a real
A-vs-C comparison still needs the persona/response-simulation layer §17.1-
§17.2 describe (and the swept parameters `eval/PREREGISTRATION.md` declares
ranges for but doesn't yet have a runner to sweep) — not built in this
session; see docs/LIMITATIONS.md. What's here is the schedule itself,
correct and tested in isolation, ready for that runner to call.
"""

from __future__ import annotations

from dataclasses import dataclass

FIXED_SCHEDULE_DAYS: tuple[int, ...] = (1, 7, 14, 21)
"""Touch on these days-since-due, and no others. A fixed schedule, not
diagnosis-driven — Arm A's whole point as a control (§17.4)."""


@dataclass(frozen=True, slots=True)
class ScheduledTouch:
    day: int
    action_type: str
    is_final: bool


def touch_for_day(days_since_due: int) -> ScheduledTouch | None:
    """None on any day not in the fixed schedule -- Arm A does not react to
    anything, which is the property that makes it a control."""
    if days_since_due not in FIXED_SCHEDULE_DAYS:
        return None
    is_final = days_since_due == FIXED_SCHEDULE_DAYS[-1]
    return ScheduledTouch(
        day=days_since_due,
        action_type="send_statutory_notice" if is_final else "send_reminder",
        is_final=is_final,
    )


def all_touches_up_to(days_since_due: int) -> list[ScheduledTouch]:
    """Every touch Arm A would have fired by this many days since due —
    for reconstructing "how many touches has this debtor received" without
    re-deriving it from a live clock."""
    return [
        touch for day in FIXED_SCHEDULE_DAYS if day <= days_since_due
        for touch in [touch_for_day(day)] if touch is not None
    ]
