"""Extractor drift — the Auditor's third job, DEVDOC_v6 §11.7. Was not
implemented ("needs a live model producing real extractions to sample and
re-run, and none exists in this build") — that blocker no longer holds:
Path B (agent.diagnose.llm_extract) is live and, given an ExtractionLog,
now records what it produces.

Samples k% of logged extractions, re-runs each one's `reply_text` against
a **second model** — the literal "(or a second model where available)"
DEVDOC_v6 offers as the alternative to a second prompt version, simpler to
build and keep in sync than authoring and maintaining two meaningfully
different prompts — and measures family/class agreement against the
original. Below threshold, the extractor is quarantined: the flag
`agent.diagnose.objection.compute_objection_marker`'s own
`extractor_quarantined` parameter already consumes, unchanged by this
module — wiring a real producer was always meant to be a config/storage
change, not a new consumer to build, and that's exactly what this is.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable

import anthropic

from agent.auditor.extraction_log import ExtractionLog, LoggedExtraction
from agent.diagnose.extract import ExtractionResult
from agent.diagnose.llm_extract import ExtractionFailed, extract_from_reply
from agent.spend import SpendLedger

DEFAULT_EXTRACTOR_SAMPLE_RATE = 0.10
"""Matches the config default DEVDOC_v6 §11.7 names — a starting point,
not a finding, same status as the bounds-integrity sample rate."""

DEFAULT_AGREEMENT_THRESHOLD = 0.80
"""Not given a specific value in DEVDOC_v6 beyond "below threshold" — a
starting default, same spirit as agent.mandate.health's issuer-failure-rate
threshold: cheap to raise once real drift-rate data exists."""

SECOND_OPINION_MODEL = "claude-haiku-4-5-20251001"
"""A different model *family* from llm_extract.DEFAULT_MODEL (Sonnet 5),
not just a different prompt against the same model — the literal reading
of "a second model where available"."""

RerunFn = Callable[[str], ExtractionResult]


@dataclass(frozen=True, slots=True)
class DriftCheckResult:
    original: LoggedExtraction
    rerun_family: str | None
    """None only if the re-run itself failed (ExtractionFailed) — treated
    as disagreement, not skipped, since a re-run that can't even produce a
    result is exactly the kind of drift this job exists to catch."""
    rerun_class: str | None
    agrees: bool


@dataclass(frozen=True, slots=True)
class ExtractorDriftReport:
    sampled: list[DriftCheckResult]
    agreement_rate: float
    quarantine: bool


def sample_logged_extractions(
    log: ExtractionLog, *, sample_rate: float = DEFAULT_EXTRACTOR_SAMPLE_RATE, rng: random.Random | None = None,
) -> list[LoggedExtraction]:
    candidates = log.all_entries()
    if not candidates:
        return []
    sample_size = max(1, round(len(candidates) * sample_rate))
    if sample_size >= len(candidates):
        return candidates
    return (rng or random.Random()).sample(candidates, sample_size)


def _default_rerun(
    *, client: anthropic.Anthropic | None, model: str, spend_ledger: SpendLedger | None,
) -> RerunFn:
    def rerun(reply_text: str) -> ExtractionResult:
        return extract_from_reply(
            reply_text, client=client, model=model, spend_ledger=spend_ledger,
            purpose="auditor_extractor_drift_recheck",
        )
    return rerun


def check_extractor_drift(
    log: ExtractionLog,
    *,
    sample_rate: float = DEFAULT_EXTRACTOR_SAMPLE_RATE,
    agreement_threshold: float = DEFAULT_AGREEMENT_THRESHOLD,
    rerun: RerunFn | None = None,
    second_opinion_model: str = SECOND_OPINION_MODEL,
    client: anthropic.Anthropic | None = None,
    spend_ledger: SpendLedger | None = None,
    rng: random.Random | None = None,
) -> ExtractorDriftReport:
    """`rerun` is injectable so a test never needs a live model or a mocked
    Anthropic client shaped just right for N different sampled replies —
    pass a plain function. Left unset, the real path re-checks each
    reply_text against `second_opinion_model` through the same, real,
    budget-gated `extract_from_reply()` every Path B call already uses —
    a real re-check costs real money, the same honest cost DEVDOC_v6 §11.7
    itself expects ("cheap to raise once the real re-check cost is
    known")."""
    active_rerun = rerun or _default_rerun(client=client, model=second_opinion_model, spend_ledger=spend_ledger)
    sampled_entries = sample_logged_extractions(log, sample_rate=sample_rate, rng=rng)

    results: list[DriftCheckResult] = []
    for entry in sampled_entries:
        try:
            reextracted = active_rerun(entry.reply_text)
            agrees = reextracted.family == entry.family and reextracted.class_ == entry.class_
            results.append(DriftCheckResult(
                original=entry, rerun_family=reextracted.family.value, rerun_class=reextracted.class_.value,
                agrees=agrees,
            ))
        except ExtractionFailed:
            results.append(DriftCheckResult(original=entry, rerun_family=None, rerun_class=None, agrees=False))

    if not results:
        return ExtractorDriftReport(sampled=[], agreement_rate=1.0, quarantine=False)

    agreement_rate = sum(1 for r in results if r.agrees) / len(results)
    return ExtractorDriftReport(
        sampled=results, agreement_rate=agreement_rate, quarantine=agreement_rate < agreement_threshold,
    )
