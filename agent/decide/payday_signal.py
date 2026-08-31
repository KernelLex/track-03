"""Payday signal: a free, already-owned alternative to peeking at a real
bank balance before debiting -- which no API exposes to a merchant. There
is no "check the customer's balance first" call anywhere in Razorpay's (or
any Indian rail's) surface, and a small probe debit against a mandate
doesn't work either -- a probe is itself an attempt, and NPCI's per-mandate
attempt cap (see PROGRESS.md) means a probe just burns a slot that could
have been the real debit.

What this build *does* already have, for free: `recovery_ledger` records
`recorded_at` for every past `captured` payment, per debtor (Law 7). The
day-of-month distribution of a debtor's own past successful captures is a
real, if weak, signal for when a future debit from that same debtor is
more likely to clear -- sourced entirely from data already collected, no
new dependency, no new consent flow.

**What this is not**: a balance check. `recorded_at` is when this build's
own SETTLE stage attributed the payment, which can lag the real debit
moment by hours, and a debtor with few or no past captures gets an
honestly low-confidence signal, not a guess dressed up as one. The actual
answer -- India's Account Aggregator framework, consent-based access to
real balance data -- is out of scope for this build (too large a lift);
this module is the free, already-available fallback in the meantime, not
a replacement for it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from agent.ledger.recovery import RecoveryLedger

MIN_SAMPLES_FOR_SIGNAL = 2
"""One past captured payment is an anecdote, not a pattern. Below this,
`likely_days` stays empty and `confidence` stays 0.0 rather than treating a
single data point as a schedule."""


@dataclass(frozen=True, slots=True)
class PaydaySignal:
    debtor_id: str
    sample_size: int
    observed_days: tuple[int, ...]
    """Day-of-month (1-31) of every past captured payment for this debtor,
    in chronological order -- the raw evidence, kept alongside the derived
    fields so a caller can inspect it rather than trust the summary blind."""
    likely_days: tuple[int, ...]
    """Days that recur (count > 1) across observed_days, most frequent
    first. Empty below MIN_SAMPLES_FOR_SIGNAL or when every past capture
    landed on a different day (no recurring pattern to report)."""
    confidence: float
    """Fraction of observed_days that landed on a likely_day. 0.0 when
    likely_days is empty. Not a probability of the *next* debit clearing --
    a description of how consistent this debtor's own history has been."""

    def favors(self, day_of_month: int) -> bool:
        """True if `day_of_month` is one of this debtor's recurring days --
        a cheap boolean for a caller that just wants a nudge, not the full
        signal."""
        return day_of_month in self.likely_days


def compute_payday_signal(recovery_ledger: RecoveryLedger, debtor_id: str) -> PaydaySignal:
    """Pure read over recovery_ledger -- no model, no external call, no new
    data collected. Safe to call as often as needed; it costs one SQL query."""
    entries = recovery_ledger.entries_for_debtor(debtor_id)
    observed_days = tuple(datetime.fromisoformat(e.recorded_at).day for e in entries)

    if len(observed_days) < MIN_SAMPLES_FOR_SIGNAL:
        return PaydaySignal(
            debtor_id=debtor_id, sample_size=len(observed_days),
            observed_days=observed_days, likely_days=(), confidence=0.0,
        )

    counts = Counter(observed_days)
    likely_days = tuple(day for day, count in counts.most_common() if count > 1)
    if not likely_days:
        return PaydaySignal(
            debtor_id=debtor_id, sample_size=len(observed_days),
            observed_days=observed_days, likely_days=(), confidence=0.0,
        )

    on_a_likely_day = sum(1 for d in observed_days if d in likely_days)
    confidence = on_a_likely_day / len(observed_days)

    return PaydaySignal(
        debtor_id=debtor_id, sample_size=len(observed_days),
        observed_days=observed_days, likely_days=likely_days, confidence=confidence,
    )
