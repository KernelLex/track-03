"""The Auditor's scheduled jobs: correct logging behaviour on both a clean
and a broken ledger, and that the scheduler itself starts, registers both
jobs, and shuts down cleanly. DEVDOC_v6 §11.7, §19."""

from __future__ import annotations

import json
import logging
import sqlite3

from agent.act.actions import ActionType
from agent.act.executor import OutboundActionStore, execute_action
from agent.auditor.extraction_log import ExtractionLog
from agent.auditor.scheduler import (
    add_extractor_drift_job,
    run_bounds_integrity_once,
    run_chain_integrity_once,
    run_extractor_drift_once,
    start_auditor_scheduler,
)
from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family
from agent.bounds.context import ActionCtx, BoundsContext, ConfigCtx, DebtorCtx, DecisionCtx, InvoiceCtx, MandateCtx
from agent.ledger.store import Ledger
from agent.rails.simulated import SimulatedRail


def passing_ctx() -> BoundsContext:
    return BoundsContext(
        debtor=DebtorCtx(id="d1", state="ENGAGED"), mandate=MandateCtx(),
        action=ActionCtx(type="create_payment_link", channel="email", rail_tag="simulated"),
        decision=DecisionCtx(ev_paise=1000), invoice=InvoiceCtx(id="inv1"), config=ConfigCtx(),
    )


def _populated_db(tmp_path) -> str:
    db_path = str(tmp_path / "ledger.db")
    rail = SimulatedRail(webhook_secret="scheduler-test")
    with Ledger(db_path) as ledger, OutboundActionStore(tmp_path / "outbound.db") as store:
        execute_action(
            action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="d1", invoice_id="inv1",
            decision_seq=1, bounds_context=passing_ctx(), rail=rail, outbound_store=store, ledger=ledger,
            payload={"amount_paise": 10_000, "description": "x"},
        )
    return db_path


def test_chain_integrity_job_logs_ok_on_a_clean_ledger(tmp_path, caplog):
    db_path = _populated_db(tmp_path)
    with caplog.at_level(logging.INFO, logger="trucommit.auditor"):
        run_chain_integrity_once(db_path)
    assert any("chain integrity OK" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.CRITICAL for r in caplog.records)


def test_chain_integrity_job_logs_critical_on_a_broken_ledger(tmp_path, caplog):
    db_path = _populated_db(tmp_path)
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT body_json FROM ledger WHERE seq = 1").fetchone()
    body = json.loads(row[0])
    body["actor"] = "TAMPERED"
    conn.execute("UPDATE ledger SET body_json = ? WHERE seq = 1", (json.dumps(body),))
    conn.commit()
    conn.close()

    with caplog.at_level(logging.INFO, logger="trucommit.auditor"):
        run_chain_integrity_once(db_path)
    assert any(r.levelno == logging.CRITICAL and "CHAIN INTEGRITY BREACH" in r.message for r in caplog.records)


def test_bounds_integrity_job_logs_ok_on_a_clean_ledger(tmp_path, caplog):
    db_path = _populated_db(tmp_path)
    with caplog.at_level(logging.INFO, logger="trucommit.auditor"):
        run_bounds_integrity_once(db_path, sample_rate=1.0)
    assert any("bounds integrity OK" in r.message for r in caplog.records)


def test_bounds_integrity_job_logs_critical_on_a_forged_verdict(tmp_path, caplog):
    db_path = _populated_db(tmp_path)
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT body_json FROM ledger WHERE seq = 1").fetchone()
    body = json.loads(row[0])
    for check in body["bounds_checks"]:
        if check["rule_id"] == "EV_FLOOR":
            check["verdict"] = "REFUSE"
    conn.execute("UPDATE ledger SET body_json = ? WHERE seq = 1", (json.dumps(body),))
    conn.commit()
    conn.close()

    with caplog.at_level(logging.INFO, logger="trucommit.auditor"):
        run_bounds_integrity_once(db_path, sample_rate=1.0)
    assert any(r.levelno == logging.CRITICAL and "BOUNDS INTEGRITY BREACH" in r.message for r in caplog.records)


def test_scheduler_starts_registers_both_jobs_and_shuts_down_cleanly(tmp_path):
    db_path = _populated_db(tmp_path)
    scheduler = start_auditor_scheduler(db_path, chain_interval_seconds=3600, bounds_interval_seconds=3600)
    try:
        assert scheduler.running
        job_ids = {job.id for job in scheduler.get_jobs()}
        assert job_ids == {"chain_integrity", "bounds_integrity"}
        # Extractor drift is deliberately NOT auto-registered -- it spends
        # real money per real run, unlike the two free jobs above.
        assert "extractor_drift" not in job_ids
    finally:
        scheduler.shutdown(wait=False)
    assert not scheduler.running


def _result(**overrides) -> ExtractionResult:
    defaults = dict(family=Family.C, **{"class": DiagnosisClass.PROMISE_STATED}, confidence=0.8)
    defaults.update(overrides)
    return ExtractionResult(**defaults)


def test_extractor_drift_job_logs_nothing_to_sample_on_an_empty_log(tmp_path, caplog):
    log_path = str(tmp_path / "extraction_log.db")
    with caplog.at_level(logging.INFO, logger="trucommit.auditor"):
        run_extractor_drift_once(log_path)
    assert any("nothing to sample yet" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.CRITICAL for r in caplog.records)


def test_extractor_drift_job_logs_ok_on_agreement(tmp_path, caplog):
    log_path = str(tmp_path / "extraction_log.db")
    with ExtractionLog(log_path) as log:
        log.record(reply_text="will pay Friday", result=_result(), model="claude-sonnet-5", purpose="path_b_extraction")

    with caplog.at_level(logging.INFO, logger="trucommit.auditor"):
        run_extractor_drift_once(log_path, sample_rate=1.0, rerun=lambda text: _result())
    assert any("extractor drift OK" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.CRITICAL for r in caplog.records)


def test_extractor_drift_job_logs_critical_on_quarantine(tmp_path, caplog):
    log_path = str(tmp_path / "extraction_log.db")
    with ExtractionLog(log_path) as log:
        log.record(reply_text="will pay Friday", result=_result(), model="claude-sonnet-5", purpose="path_b_extraction")

    disagreeing = _result(family=Family.D, **{"class": DiagnosisClass.AMOUNT})
    with caplog.at_level(logging.INFO, logger="trucommit.auditor"):
        run_extractor_drift_once(log_path, sample_rate=1.0, rerun=lambda text: disagreeing)
    assert any(r.levelno == logging.CRITICAL and "EXTRACTOR DRIFT QUARANTINE" in r.message for r in caplog.records)


def test_add_extractor_drift_job_registers_it_as_opt_in(tmp_path):
    db_path = _populated_db(tmp_path)
    log_path = str(tmp_path / "extraction_log.db")
    scheduler = start_auditor_scheduler(db_path, chain_interval_seconds=3600, bounds_interval_seconds=3600)
    try:
        add_extractor_drift_job(scheduler, log_path, interval_seconds=3600)
        job_ids = {job.id for job in scheduler.get_jobs()}
        assert "extractor_drift" in job_ids
    finally:
        scheduler.shutdown(wait=False)
