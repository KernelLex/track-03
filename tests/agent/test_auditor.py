"""The Auditor's two model-free jobs: chain integrity (a thin wrap over
Ledger.verify_chain()) and bounds integrity (recompute check_bounds() from
each action's own recorded inputs, catch a gate that silently stopped
matching what it once recorded). DEVDOC_v6 §11.7."""

from __future__ import annotations

import json
import random
import sqlite3

import pytest

from agent.act.actions import ActionType
from agent.act.executor import OutboundActionStore, execute_action
from agent.auditor.auditor import (
    BoundsIntegrityBreach,
    check_bounds_integrity,
    check_bounds_integrity_or_raise,
    check_chain_integrity,
    sample_executed_actions,
)
from agent.bounds.context import ActionCtx, BoundsContext, ConfigCtx, DebtorCtx, DecisionCtx, InvoiceCtx, MandateCtx
from agent.ledger.store import ChainIntegrityError, Ledger
from agent.rails.simulated import SimulatedRail


def passing_ctx(**overrides) -> BoundsContext:
    defaults = dict(
        debtor=DebtorCtx(id="debtor_1", state="ENGAGED"),
        mandate=MandateCtx(),
        action=ActionCtx(type="create_payment_link", channel="email", rail_tag="simulated"),
        decision=DecisionCtx(ev_paise=1000),
        invoice=InvoiceCtx(id="inv_1"),
        config=ConfigCtx(),
    )
    defaults.update(overrides)
    return BoundsContext(**defaults)


@pytest.fixture
def populated_ledger(tmp_path):
    rail = SimulatedRail(webhook_secret="auditor-test")
    with Ledger(tmp_path / "ledger.db") as ledger, OutboundActionStore(tmp_path / "outbound.db") as store:
        for seq in range(5):
            execute_action(
                action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id=f"inv_{seq}",
                decision_seq=seq, bounds_context=passing_ctx(), rail=rail, outbound_store=store, ledger=ledger,
                payload={"amount_paise": 10_000 + seq, "description": f"invoice {seq}"},
            )
        yield tmp_path / "ledger.db"


# ---- Chain integrity ----


def test_chain_integrity_passes_on_a_clean_ledger(populated_ledger):
    with Ledger(populated_ledger) as ledger:
        check_chain_integrity(ledger)  # must not raise


def test_chain_integrity_raises_on_a_tampered_ledger(populated_ledger):
    conn = sqlite3.connect(str(populated_ledger))
    row = conn.execute("SELECT body_json FROM ledger WHERE seq = 2").fetchone()
    body = json.loads(row[0])
    body["actor"] = "TAMPERED"
    conn.execute("UPDATE ledger SET body_json = ? WHERE seq = 2", (json.dumps(body),))
    conn.commit()
    conn.close()

    with Ledger(populated_ledger) as ledger:
        with pytest.raises(ChainIntegrityError):
            check_chain_integrity(ledger)


# ---- Bounds integrity ----


def test_bounds_integrity_finds_no_violations_on_an_untampered_ledger(populated_ledger):
    with Ledger(populated_ledger) as ledger:
        violations = check_bounds_integrity(ledger, sample_rate=1.0)
    assert violations == []


def test_bounds_integrity_or_raise_does_not_raise_when_clean(populated_ledger):
    with Ledger(populated_ledger) as ledger:
        check_bounds_integrity_or_raise(ledger, sample_rate=1.0)  # must not raise


def test_bounds_integrity_catches_a_gate_that_silently_stopped_matching(populated_ledger):
    """Simulates exactly the failure mode §11.7 exists to catch: someone (a
    bug, a bypass) recorded a PASS verdict for an action whose own inputs
    would refuse it. Tamper the recorded bounds_checks directly, not the
    snapshot -- the snapshot is "what really happened", the recorded verdict
    is the thing that can silently drift from it."""
    conn = sqlite3.connect(str(populated_ledger))
    row = conn.execute("SELECT body_json FROM ledger WHERE seq = 3").fetchone()
    body = json.loads(row[0])
    for check in body["bounds_checks"]:
        if check["rule_id"] == "EV_FLOOR":
            check["verdict"] = "REFUSE"  # this action actually passed EV_FLOOR; claim it didn't
            check["reason"] = "forged refusal"
    conn.execute("UPDATE ledger SET body_json = ? WHERE seq = 3", (json.dumps(body),))
    conn.commit()
    conn.close()

    with Ledger(populated_ledger) as ledger:
        violations = check_bounds_integrity(ledger, sample_rate=1.0)
        assert len(violations) == 1
        assert violations[0].seq == 3

        with pytest.raises(BoundsIntegrityBreach) as exc_info:
            check_bounds_integrity_or_raise(ledger, sample_rate=1.0)
        assert "3" in str(exc_info.value)


def test_sample_size_is_at_least_one_when_any_candidates_exist(populated_ledger):
    with Ledger(populated_ledger) as ledger:
        sample = sample_executed_actions(ledger, sample_rate=0.01, rng=random.Random(42))
    assert len(sample) >= 1


def test_full_sample_rate_returns_every_candidate(populated_ledger):
    with Ledger(populated_ledger) as ledger:
        sample = sample_executed_actions(ledger, sample_rate=1.0)
    assert len(sample) == 5


def test_empty_ledger_has_nothing_to_sample(tmp_path):
    with Ledger(tmp_path / "empty.db") as ledger:
        assert sample_executed_actions(ledger) == []
        assert check_bounds_integrity(ledger) == []
