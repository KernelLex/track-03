"""Ledger tests: hash chain integrity, tamper detection naming the exact seq, and replay. DEVDOC_v6 §15."""

from __future__ import annotations

import json
import sqlite3

import pytest

from agent.ledger.models import Fact, LedgerEntry, Provenance, ProvenanceViolation, assert_legal_provenance
from agent.ledger.store import ChainIntegrityError, GENESIS_HASH, Ledger


@pytest.fixture
def ledger(tmp_path):
    db_path = tmp_path / "ledger.db"
    with Ledger(db_path) as lg:
        yield lg


def make_entry(debtor_id: str = "debtor-1", **overrides) -> LedgerEntry:
    defaults = dict(actor="DIAGNOSE", debtor_id=debtor_id)
    defaults.update(overrides)
    return LedgerEntry(**defaults)


def test_first_entry_chains_from_genesis(ledger):
    finalized = ledger.append(make_entry())
    assert finalized.seq == 1
    assert finalized.prev_hash == GENESIS_HASH
    assert finalized.hash is not None
    assert finalized.ts is not None


def test_chain_links_sequentially(ledger):
    first = ledger.append(make_entry())
    second = ledger.append(make_entry())
    third = ledger.append(make_entry())

    assert second.prev_hash == first.hash
    assert third.prev_hash == second.hash
    assert [e.seq for e in (first, second, third)] == [1, 2, 3]


def test_verify_chain_passes_on_untampered_ledger(ledger):
    for _ in range(5):
        ledger.append(make_entry())
    ledger.verify_chain()  # must not raise


def test_tamper_detection_identifies_exact_seq(ledger, tmp_path):
    for _ in range(5):
        ledger.append(make_entry())

    # Directly mutate row seq=3's payload, bypassing the Ledger API entirely —
    # this simulates an attacker (or a bug) writing straight to the db file.
    conn = sqlite3.connect(ledger.db_path)
    row = conn.execute("SELECT body_json FROM ledger WHERE seq = 3").fetchone()
    body = json.loads(row[0])
    body["actor"] = "TAMPERED"
    conn.execute("UPDATE ledger SET body_json = ? WHERE seq = 3", (json.dumps(body),))
    conn.commit()
    conn.close()

    with pytest.raises(ChainIntegrityError) as exc_info:
        ledger.verify_chain()
    assert "seq=3" in str(exc_info.value)


def test_tamper_of_prev_hash_also_caught_at_that_seq(ledger):
    for _ in range(4):
        ledger.append(make_entry())

    conn = sqlite3.connect(ledger.db_path)
    conn.execute("UPDATE ledger SET prev_hash = ? WHERE seq = 2", ("f" * 64,))
    conn.commit()
    conn.close()

    with pytest.raises(ChainIntegrityError) as exc_info:
        ledger.verify_chain()
    assert "seq=2" in str(exc_info.value)


def test_replay_reconstructs_one_debtor_from_an_interleaved_chain(ledger):
    ledger.append(make_entry(debtor_id="alice", outcome={"debtor_state_after": "AT_RISK"}))
    ledger.append(make_entry(debtor_id="bob", outcome={"debtor_state_after": "AT_RISK"}))
    ledger.append(make_entry(debtor_id="alice", outcome={"debtor_state_after": "DIAGNOSED"}))
    ledger.append(make_entry(debtor_id="bob", outcome={"debtor_state_after": "RECOVERED", "recovered_paise": 50_000}))
    ledger.append(make_entry(debtor_id="alice", outcome={"debtor_state_after": "RECOVERED", "recovered_paise": 10_000}))

    alice = ledger.replay("alice")
    assert [e.debtor_id for e in alice.entries] == ["alice", "alice", "alice"]
    assert alice.current_state == "RECOVERED"
    assert alice.recovered_paise == 10_000

    bob = ledger.replay("bob")
    assert bob.current_state == "RECOVERED"
    assert bob.recovered_paise == 50_000


def test_replay_until_timestamp_excludes_later_entries(ledger):
    first = ledger.append(make_entry(outcome={"debtor_state_after": "AT_RISK"}))
    second = ledger.append(make_entry(outcome={"debtor_state_after": "DIAGNOSED"}))
    ledger.append(make_entry(outcome={"debtor_state_after": "RECOVERED"}))

    result = ledger.replay("debtor-1", until=first.ts)
    assert result.current_state == "AT_RISK"

    result = ledger.replay("debtor-1", until=second.ts)
    assert result.current_state == "DIAGNOSED"


def test_replay_of_unknown_debtor_is_empty_not_an_error(ledger):
    ledger.append(make_entry(debtor_id="alice"))
    result = ledger.replay("nobody")
    assert result.entries == ()
    assert result.current_state is None
    assert result.recovered_paise == 0


def test_shuffled_thrice_replay_is_order_independent_within_one_debtor(tmp_path):
    """A weaker cousin of §9.5's full shuffled-thrice test: replay's fold is
    order-independent for *this* debtor's own outcomes regardless of how many
    other debtors' entries are interleaved between them, because it filters
    by debtor_id before folding. The full §9.5 test (idempotent re-ingestion
    of a shuffled webhook stream) lives with the ingest module once built."""
    import random

    totals = []
    for _ in range(3):
        db_path = tmp_path / f"ledger_{random.random()}.db"
        with Ledger(db_path) as lg:
            events = (
                [("alice", i) for i in range(5)] + [("bob", i) for i in range(5)]
            )
            random.shuffle(events)
            for debtor, i in events:
                lg.append(make_entry(debtor_id=debtor, outcome={"recovered_paise": 100 * i}))
            totals.append(lg.replay("alice").recovered_paise)

    assert len(set(totals)) == 1  # same total regardless of interleaving order


# --- Fact provenance (§8) ---


def test_legal_computation_guard_passes_system_and_human_facts():
    facts = [
        Fact(name="acceptance_date", value="2026-01-01", provenance=Provenance.SYSTEM),
        Fact(name="approved_by", value="ops@supplier.example", provenance=Provenance.HUMAN),
    ]
    assert_legal_provenance(facts)  # must not raise


def test_legal_computation_guard_crashes_on_model_provenance():
    facts = [
        Fact(name="acceptance_date", value="2026-01-01", provenance=Provenance.SYSTEM),
        Fact(name="possible_objection_present", value=True, provenance=Provenance.MODEL, source_ref="extract-42"),
    ]
    with pytest.raises(ProvenanceViolation) as exc_info:
        assert_legal_provenance(facts)
    assert "possible_objection_present" in str(exc_info.value)
