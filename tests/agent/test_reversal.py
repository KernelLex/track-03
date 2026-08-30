"""Law 9: every money-moving action has a named inverse, and reversals are never
silently netted into recovery. DEVDOC_v6 §11.6."""

from __future__ import annotations

import pytest

from agent.act.actions import MONEY_MOVING_ACTIONS, ActionType
from agent.act.reversal import (
    NoReversalDefined,
    REVERSAL_MAP,
    ReversalGate,
    ReversalsLedger,
    reversal_gate_for,
)
from agent.ledger.recovery import RecoveryLedger


def test_every_money_moving_action_has_a_reversal_defined():
    for action in MONEY_MOVING_ACTIONS:
        assert action in REVERSAL_MAP, f"Law 9 violated: {action.value} has no reversal"


def test_reissue_artifact_also_has_a_reversal_even_though_it_moves_no_money():
    """Conspicuous-by-absence, not strictly Law-9-required (§11.6) — checked
    separately from the MONEY_MOVING_ACTIONS loop above."""
    assert ActionType.REISSUE_ARTIFACT in REVERSAL_MAP


def test_create_mandate_reversal_is_human_gated_by_default():
    assert reversal_gate_for(ActionType.CREATE_MANDATE) == ReversalGate.HUMAN


def test_create_mandate_reversal_becomes_autonomous_on_debtor_optout():
    """The 2026 framework requires opt-out be honoured — refusing to reverse a
    mandate the debtor just opted out of would itself be a violation (§11.6)."""
    assert reversal_gate_for(
        ActionType.CREATE_MANDATE, debtor_opted_out_this_cycle=True
    ) == ReversalGate.AUTONOMOUS


def test_retry_charge_reversal_is_human_gated():
    assert reversal_gate_for(ActionType.RETRY_CHARGE) == ReversalGate.HUMAN


def test_reissue_artifact_reversal_is_autonomous():
    assert reversal_gate_for(ActionType.REISSUE_ARTIFACT) == ReversalGate.AUTONOMOUS


def test_an_action_with_no_reversal_defined_raises_rather_than_assuming_one():
    class _FakeAction:
        value = "not_a_real_action"

    with pytest.raises(NoReversalDefined):
        reversal_gate_for(_FakeAction())  # type: ignore[arg-type]


# ---- Reversals ledger: separate from recovery, linked by reverses_seq ----


def test_reversal_is_recorded_separately_from_recovery(tmp_path):
    with RecoveryLedger(tmp_path / "recovery.db") as recovery, \
         ReversalsLedger(tmp_path / "reversals.db") as reversals:

        recovery.attribute(
            payment_id="pay_erroneous", payment_status="captured", invoice_id="inv_1",
            debtor_id="debtor_1", amount_paise=10_000, rail_tag="simulated",
        )
        assert recovery.total_recovered_paise() == 10_000

        entry = reversals.record(
            original_action_type="retry_charge", reverses_seq=42,
            amount_paise=10_000, reason="erroneous debit — wrong debtor matched",
        )

        # The two totals are independent — nothing here nets one into the other.
        assert recovery.total_recovered_paise() == 10_000
        assert reversals.total_reversed_paise() == 10_000
        assert entry.reverses_seq == 42


def test_reversals_ledger_links_back_to_the_original_ledger_seq(tmp_path):
    with ReversalsLedger(tmp_path / "reversals.db") as reversals:
        entry = reversals.record(
            original_action_type="create_mandate", reverses_seq=7,
            amount_paise=5_000, reason="debtor opted out this cycle",
        )
        [stored] = reversals.all_entries()
        assert stored.reverses_seq == 7 == entry.reverses_seq


def test_reversal_amount_must_be_positive(tmp_path):
    with ReversalsLedger(tmp_path / "reversals.db") as reversals:
        with pytest.raises(ValueError):
            reversals.record(original_action_type="retry_charge", reverses_seq=1, amount_paise=0, reason="x")
