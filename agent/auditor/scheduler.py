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


def start_auditor_scheduler(
    db_path: str,
    *,
    chain_interval_seconds: int = 300,
    bounds_interval_seconds: int = 900,
    bounds_sample_rate: float = 0.10,
) -> BackgroundScheduler:
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
