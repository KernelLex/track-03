"""§8's worked example, as real code: deemed acceptance with the model as veto
only, never as the establisher of a legal fact.

The naive chain (LLM reads the thread -> concludes no objection -> clock
starts -> interest accrues) is a Law 2 violation. The correct chain below
computes `deemed_accepted` from SYSTEM facts and a record query
(`possible_objection_present`), with the model's role limited to setting
`objection_marker` -- and the asymmetry is deliberate: a false positive here
costs a human review; a false negative cannot produce a wrong legal claim,
because uncertainty always sets the marker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from agent.diagnose.extract import ExtractionResult, Family

OBJECTION_LEXICON: frozenset[str] = frozenset({
    "dispute", "short supply", "damaged", "not as per po", "mismatch",
    "galat", "kam hai", "nahi mila",
})


def text_matches_objection_lexicon(raw_text: str) -> bool:
    lowered = raw_text.lower()
    return any(term in lowered for term in OBJECTION_LEXICON)


def compute_objection_marker(
    extraction: ExtractionResult,
    *,
    raw_text: str,
    confidence_threshold: float,
    extractor_quarantined: bool,
) -> bool:
    """objection_marker is TRUE if *any* of: family D, low confidence, a lexicon
    hit, or the extractor is quarantined (§11.7's Auditor trip). Deliberately
    an OR of independent, cheap-to-trigger conditions -- optimised for recall,
    per §8's own framing, because a false positive here is a human review and
    a false negative is a wrong legal claim."""
    if extractor_quarantined:
        return True
    if extraction.family == Family.D:
        return True
    if extraction.confidence < confidence_threshold:
        return True
    if text_matches_objection_lexicon(raw_text):
        return True
    return False


@dataclass(frozen=True, slots=True)
class CommsLogEntry:
    direction: Literal["inbound", "outbound"]
    received_at: datetime | None
    objection_marker: bool
    is_regulatory_notice: bool = False


def possible_objection_present(
    entries: list[CommsLogEntry], *, window_start: datetime, window_end: datetime
) -> bool:
    """The record query from §8 step 3, expressed over an in-memory list rather
    than SQL -- same predicate, same semantics: EXISTS an inbound message in
    the window with objection_marker set."""
    return any(
        e.direction == "inbound"
        and e.received_at is not None
        and window_start <= e.received_at <= window_end
        and e.objection_marker
        for e in entries
    )


@dataclass(frozen=True, slots=True)
class DeemedAcceptanceResult:
    deemed_accepted: bool
    escalate_to_human_queue: bool
    """True whenever deemed_accepted is False -- §8: "Any marker, uncertainty
    or quarantine -> HUMAN_QUEUE." There is no third outcome."""


def compute_deemed_acceptance(
    *,
    acceptance_date: datetime,
    objection_window_days: int,
    comms: list[CommsLogEntry],
) -> DeemedAcceptanceResult:
    """`acceptance_date` and `objection_window_days` must both be SYSTEM-provenance
    facts (a delivery/acceptance record and Section 2(b) MSMED Act's 15 days,
    respectively) -- this function takes them as plain values because provenance
    is the *caller's* obligation to have already checked (agent.ledger.models
    .assert_legal_provenance), not something re-derivable from a datetime."""
    window_end = acceptance_date + timedelta(days=objection_window_days)
    if possible_objection_present(comms, window_start=acceptance_date, window_end=window_end):
        return DeemedAcceptanceResult(deemed_accepted=False, escalate_to_human_queue=True)
    return DeemedAcceptanceResult(deemed_accepted=True, escalate_to_human_queue=False)
