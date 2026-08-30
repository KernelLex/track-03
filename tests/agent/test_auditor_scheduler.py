"""The Auditor's scheduled jobs: correct logging behaviour on both a clean
and a broken ledger, and that the scheduler itself starts, registers both
jobs, and shuts down cleanly. DEVDOC_v6 §11.7, §19."""

from __future__ import annotations

import json
import logging
import sqlite3

from agent.act.actions import ActionType
from agent.act.executor import OutboundActionStore, execute_action
from agent.auditor.scheduler import run_bounds_integrity_once, run_chain_integrity_once, start_auditor_scheduler
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
    finally:
        scheduler.shutdown(wait=False)
    assert not scheduler.running
