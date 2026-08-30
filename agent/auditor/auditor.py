"""The Auditor: read-only, out-of-band, never on the path it audits. DEVDOC_v6 §11.7.

Two of its three jobs need no model and are built here:

- **Chain integrity** is exactly `Ledger.verify_chain()` — wrapped for a
  single entry point, not reimplemented.
- **Bounds integrity** recomputes `check_bounds()` from a ledger entry's own
  recorded `bounds_context_snapshot` (agent/act/executor.py writes this on
  every dispatch) and asserts the recorded verdict still matches. This is
  "the only defence against a gate that silently stopped being called" —
  DEVDOC_v6's own words for why this job exists.

**Extractor drift is not implemented** — it needs a live model producing
real extractions to sample and re-run, and none exists in this build (see
docs/LIMITATIONS.md). The quarantine flag it would set on a trip is already
consumed downstream (`agent.diagnose.objection.compute_objection_marker`'s
`extractor_quarantined` parameter), so wiring a real producer in later is a
config/storage change, not a new consumer to build.

The Auditor never writes to `decisions` and never proposes an action.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from agent.bounds.context import BoundsContext
from agent.bounds.engine import check_bounds
from agent.ledger.models import LedgerEntry
from agent.ledger.store import Ledger

DEFAULT_BOUNDS_SAMPLE_RATE = 0.10
"""Matches the config default named in DEVDOC_v6 §11.7 — a starting point,
not a finding; cheap to raise once the real re-check cost is known."""


@dataclass(frozen=True, slots=True)
class BoundsIntegrityViolation:
    seq: int
    recorded_verdicts: list[dict]
    recomputed_verdicts: list[dict]


class BoundsIntegrityBreach(Exception):
    """A sampled action's recorded bounds verdict no longer matches what
    check_bounds() computes from that same action's own recorded inputs —
    §11.7: halt the arm, write WHAT_BROKE.md. Never swallowed."""

    def __init__(self, violations: list[BoundsIntegrityViolation]):
        self.violations = violations
        super().__init__(f"{len(violations)} bounds-integrity violation(s): seqs={[v.seq for v in violations]}")


def check_chain_integrity(ledger: Ledger) -> None:
    """The Auditor's chain-integrity job: refuse to start on a broken chain,
    naming the exact seq, rather than degrading (§28). Raises
    ChainIntegrityError — this function adds no behaviour beyond calling it,
    deliberately, since re-verifying is not this module's job to get subtly
    different from the ledger's own check."""
    ledger.verify_chain()


def _entries_with_snapshots(ledger: Ledger) -> list[LedgerEntry]:
    return [
        entry for entry in ledger.all_entries()
        if entry.action is not None and "bounds_context_snapshot" in entry.action
    ]


def sample_executed_actions(
    ledger: Ledger, *, sample_rate: float = DEFAULT_BOUNDS_SAMPLE_RATE, rng: random.Random | None = None
) -> list[LedgerEntry]:
    candidates = _entries_with_snapshots(ledger)
    if not candidates:
        return []
    sample_size = max(1, round(len(candidates) * sample_rate))
    if sample_size >= len(candidates):
        return candidates
    return (rng or random.Random()).sample(candidates, sample_size)


def check_bounds_integrity(
    ledger: Ledger, *, sample_rate: float = DEFAULT_BOUNDS_SAMPLE_RATE, rng: random.Random | None = None
) -> list[BoundsIntegrityViolation]:
    """Returns violations rather than raising — see
    check_bounds_integrity_or_raise for the halt-the-arm behaviour §11.7
    actually wants when a real trip happens."""
    violations: list[BoundsIntegrityViolation] = []
    for entry in sample_executed_actions(ledger, sample_rate=sample_rate, rng=rng):
        snapshot = entry.action["bounds_context_snapshot"]  # type: ignore[index]
        ctx = BoundsContext.from_dict(snapshot)
        recomputed = [v.to_dict() for v in check_bounds(ctx).verdicts]
        if recomputed != entry.bounds_checks:
            violations.append(BoundsIntegrityViolation(
                seq=entry.seq, recorded_verdicts=entry.bounds_checks, recomputed_verdicts=recomputed,
            ))
    return violations


def check_bounds_integrity_or_raise(
    ledger: Ledger, *, sample_rate: float = DEFAULT_BOUNDS_SAMPLE_RATE, rng: random.Random | None = None
) -> None:
    violations = check_bounds_integrity(ledger, sample_rate=sample_rate, rng=rng)
    if violations:
        raise BoundsIntegrityBreach(violations)
