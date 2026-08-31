"""Runs the Auditor's two model-free jobs on a schedule. DEVDOC_v6 §11.7, §19, §28.

APScheduler in-process — no broker, no worker, no Redis, per §19's stack
decision. Chain integrity runs frequently (cheap: one pass over the ledger's
hashes); bounds integrity runs a sample less often.

**What "on trip" means here versus what §11.7 actually wants**: the spec's
own words are "halt the arm, write WHAT_BROKE.md" — that needs an "arm"
concept from the eval harness (§17), which doesn't exist in this build (see
docs/LIMITATIONS.md). Absent that, a trip here logs at `CRITICAL` rather
than halting a process that isn't itself running an arm. Wiring a real
halt-the-arm behaviour in is a config change once §17's harness exists, not
a rewrite of this module.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from agent.auditor.auditor import BoundsIntegrityBreach, check_bounds_integrity_or_raise, check_chain_integrity
from agent.auditor.extraction_log import ExtractionLog
from agent.auditor.extractor_drift import DEFAULT_AGREEMENT_THRESHOLD, DEFAULT_EXTRACTOR_SAMPLE_RATE, check_extractor_drift
from agent.ledger.store import ChainIntegrityError, Ledger

logger = logging.getLogger("trucommit.auditor")


def run_chain_integrity_once(db_path: str) -> None:
    with Ledger(db_path) as ledger:
        try:
            check_chain_integrity(ledger)
            logger.info("chain integrity OK")
        except ChainIntegrityError as exc:
            logger.critical("CHAIN INTEGRITY BREACH: %s", exc)


def run_bounds_integrity_once(db_path: str, sample_rate: float) -> None:
    with Ledger(db_path) as ledger:
        try:
            check_bounds_integrity_or_raise(ledger, sample_rate=sample_rate)
            logger.info("bounds integrity OK")
        except BoundsIntegrityBreach as exc:
            logger.critical("BOUNDS INTEGRITY BREACH: %s", exc)


def run_extractor_drift_once(
    extraction_log_path: str,
    *,
    sample_rate: float = DEFAULT_EXTRACTOR_SAMPLE_RATE,
    agreement_threshold: float = DEFAULT_AGREEMENT_THRESHOLD,
    rerun=None,
) -> None:
    """Unlike the two jobs above, every real run of this one spends real
    money — each sampled reply_text is re-checked through a real,
    budget-gated `extract_from_reply()` call (agent.spend's own $-ceiling
    enforcement still applies; this doesn't bypass it). See
    `start_auditor_scheduler`'s own docstring for why this is not wired in
    automatically the way chain/bounds integrity are.

    `rerun` passes straight through to `check_extractor_drift` — left
    unset in production (the real, budget-gated re-check runs), injected
    in tests so this function's own logging behaviour is testable without
    a live model call."""
    with ExtractionLog(extraction_log_path) as log:
        report = check_extractor_drift(log, sample_rate=sample_rate, agreement_threshold=agreement_threshold, rerun=rerun)
    if not report.sampled:
        logger.info("extractor drift: nothing to sample yet")
    elif report.quarantine:
        logger.critical(
            "EXTRACTOR DRIFT QUARANTINE: agreement_rate=%.2f below threshold=%.2f over %d sampled",
            report.agreement_rate, agreement_threshold, len(report.sampled),
        )
    else:
        logger.info(
            "extractor drift OK: agreement_rate=%.2f over %d sampled", report.agreement_rate, len(report.sampled),
        )


def start_auditor_scheduler(
    db_path: str,
    *,
    chain_interval_seconds: int = 300,
    bounds_interval_seconds: int = 900,
    bounds_sample_rate: float = 0.10,
) -> BackgroundScheduler:
    """Starts the two model-free jobs only. Extractor drift (above) is
    deliberately NOT added here or anywhere in agent/api/app.py's
    lifespan — every real run spends real money against a hard $20
    ceiling this project has committed to (agent/spend.py), and putting a
    real-money job on an automatic timer is a decision left to whoever
    runs this server, not made silently on their behalf. Call
    `add_extractor_drift_job` explicitly to opt in."""
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_chain_integrity_once, "interval", seconds=chain_interval_seconds,
        args=[db_path], id="chain_integrity",
    )
    scheduler.add_job(
        run_bounds_integrity_once, "interval", seconds=bounds_interval_seconds,
        args=[db_path, bounds_sample_rate], id="bounds_integrity",
    )
    scheduler.start()
    return scheduler


def add_extractor_drift_job(
    scheduler: BackgroundScheduler,
    extraction_log_path: str,
    *,
    interval_seconds: int = 86_400,
    sample_rate: float = DEFAULT_EXTRACTOR_SAMPLE_RATE,
    agreement_threshold: float = DEFAULT_AGREEMENT_THRESHOLD,
) -> None:
    """Opt-in only — see start_auditor_scheduler's docstring. Defaults to
    once every 24h specifically because it's a real spend: at even a
    handful of logged extractions and a 10% sample, a daily cadence keeps
    the Auditor's own cost from being a meaningful fraction of the $20
    ceiling agent/spend.py enforces."""
    scheduler.add_job(
        run_extractor_drift_once, "interval", seconds=interval_seconds,
        args=[extraction_log_path], kwargs={"sample_rate": sample_rate, "agreement_threshold": agreement_threshold},
        id="extractor_drift",
    )
