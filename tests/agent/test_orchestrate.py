"""agent.orchestrate -- DIAGNOSE -> DECIDE -> BOUNDS -> ACT run back to back,
for real, the piece that turns the seven agents from "each individually
tested" into "actually runs as a pipeline"."""

from __future__ import annotations

import pytest

from agent.act.actions import ActionType
from agent.act.executor import OutboundActionStore
from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family
from agent.diagnose.taxonomy import FailureClassification, FailureTaxonomy, default_taxonomy
from agent.ledger.store import Ledger
from agent.notify.simulated import SimulatedChannel
from agent.diagnose.taxonomy import UnknownFailureCode
from agent.orchestrate import (
    UnmappedFailureCode,
    diagnose_from_failure_code,
    disposition_for_code,
    run_pipeline,
    select_action_for_diagnosis,
    touches_last_7_days,
)
from agent.rails.simulated import SimulatedRail


@pytest.fixture
def store(tmp_path):
    with OutboundActionStore(tmp_path / "outbound.db") as s:
        yield s


@pytest.fixture
def ledger(tmp_path):
    with Ledger(tmp_path / "ledger.db") as lg:
        yield lg


@pytest.fixture
def rail():
    return SimulatedRail(webhook_secret="test-secret")


class TestDiagnoseFromFailureCode:
    def test_every_real_taxonomy_code_has_a_mapping(self):
        """Guards against the taxonomy growing a new code nobody wires up --
        fails loudly here rather than silently defaulting in production."""
        taxonomy = default_taxonomy()
        for code, rail in taxonomy._entries.keys():
            result = diagnose_from_failure_code(code, rail, taxonomy=taxonomy)
            assert isinstance(result, ExtractionResult)

    def test_insufficient_funds_maps_to_the_matching_class(self):
        result = diagnose_from_failure_code("insufficient_funds", "cards")
        assert result.family == Family.A
        assert result.class_ == DiagnosisClass.INSUFFICIENT_FUNDS

    def test_path_a_confidence_is_always_1(self):
        """A structured failure code is a fact from the rail, not a guess --
        unlike Path B's LLM confidence, which is genuinely calibrated."""
        result = diagnose_from_failure_code("card_expired", "cards")
        assert result.confidence == 1.0

    def test_rail_is_optional_and_found_by_searching_across_rails(self):
        """A real payment.failed webhook carries error_code but not which
        rail it came from -- this must still work without one."""
        result = diagnose_from_failure_code("insufficient_funds")
        assert result.class_ == DiagnosisClass.INSUFFICIENT_FUNDS

    def test_unmapped_code_raises_rather_than_silently_defaulting(self):
        fake_taxonomy = FailureTaxonomy({
            ("totally_new_code", "cards"): FailureClassification(
                code="totally_new_code", rail="cards", source="bank",
                disposition="RETRYABLE", description="not a real code",
            )
        })
        with pytest.raises(UnmappedFailureCode):
            diagnose_from_failure_code("totally_new_code", "cards", taxonomy=fake_taxonomy)


class TestDispositionForCode:
    def test_retryable_code(self):
        assert disposition_for_code("insufficient_funds") == "RETRYABLE"

    def test_terminal_code(self):
        assert disposition_for_code("card_expired") == "TERMINAL"

    def test_explicit_rail(self):
        assert disposition_for_code("insufficient_funds", "upi") == "RETRYABLE"

    def test_unknown_code_raises(self):
        with pytest.raises(UnknownFailureCode):
            disposition_for_code("not_a_real_code")


class TestSelectActionForDiagnosis:
    def test_family_a_retryable_gets_retry_charge(self):
        diagnosis = ExtractionResult(family=Family.A, class_=DiagnosisClass.INSUFFICIENT_FUNDS, confidence=1.0)
        assert select_action_for_diagnosis(diagnosis, disposition="RETRYABLE") == ActionType.RETRY_CHARGE

    def test_family_a_terminal_gets_a_fresh_payment_link_not_a_retry(self):
        diagnosis = ExtractionResult(family=Family.A, class_=DiagnosisClass.INSTRUMENT_EXPIRED, confidence=1.0)
        assert select_action_for_diagnosis(diagnosis, disposition="TERMINAL") == ActionType.CREATE_PAYMENT_LINK

    def test_family_b_gets_reissue_artifact(self):
        diagnosis = ExtractionResult(family=Family.B, class_=DiagnosisClass.PO_MISMATCH, confidence=1.0)
        assert select_action_for_diagnosis(diagnosis) == ActionType.REISSUE_ARTIFACT

    def test_family_c_gets_send_reminder(self):
        diagnosis = ExtractionResult(family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.8)
        assert select_action_for_diagnosis(diagnosis) == ActionType.SEND_REMINDER

    def test_family_d_gets_escalate_human(self):
        diagnosis = ExtractionResult(family=Family.D, class_=DiagnosisClass.QUANTITY_QUALITY, confidence=0.9)
        assert select_action_for_diagnosis(diagnosis) == ActionType.ESCALATE_HUMAN


class TestTouchesLast7Days:
    def test_zero_for_a_debtor_with_no_history(self, ledger):
        assert touches_last_7_days(ledger, "debtor_new") == 0

    def test_counts_only_entries_with_an_action(self, ledger):
        from agent.ledger.models import LedgerEntry
        ledger.append(LedgerEntry(actor="X", debtor_id="d1", bounds_checks=[{"rule_id": "EV_FLOOR", "verdict": "REFUSE"}]))
        assert touches_last_7_days(ledger, "d1") == 0  # a refusal-only entry has action=None


class TestRunPipeline:
    def test_family_c_diagnosis_produces_a_passing_send_reminder(self, store, ledger, rail):
        diagnosis = ExtractionResult(family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.8)
        result = run_pipeline(
            debtor_id="d1", invoice_id="inv1", amount_paise=40_000_00, diagnosis=diagnosis,
            channel_tag="telegram", ledger=ledger, outbound_store=store, rail=rail,
        )
        assert result.bounds_passed is True
        assert result.action_type == ActionType.SEND_REMINDER
        assert result.action_outcome is not None
        assert result.action_outcome.detail["message_dispatched"] is True

    def test_a_real_channel_actually_gets_called_when_to_and_text_are_given(self, store, ledger, rail):
        channel = SimulatedChannel()
        diagnosis = ExtractionResult(family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.8)
        result = run_pipeline(
            debtor_id="d1", invoice_id="inv1", amount_paise=40_000_00, diagnosis=diagnosis,
            channel_tag="telegram", ledger=ledger, outbound_store=store, rail=rail,
            channel=channel, to="12345", message_text="Invoice inv1 is overdue.",
        )
        assert result.bounds_passed is True
        assert channel.sent == [{"to": "12345", "text": "Invoice inv1 is overdue."}]

    def test_family_d_escalates_and_passes(self, store, ledger, rail):
        diagnosis = ExtractionResult(family=Family.D, class_=DiagnosisClass.QUANTITY_QUALITY, confidence=0.9)
        result = run_pipeline(
            debtor_id="d2", invoice_id="inv2", amount_paise=88_000_00, diagnosis=diagnosis,
            channel_tag="telegram", ledger=ledger, outbound_store=store, rail=rail,
        )
        assert result.action_type == ActionType.ESCALATE_HUMAN
        assert result.bounds_passed is True

    def test_every_call_writes_to_the_real_ledger(self, store, ledger, rail):
        diagnosis = ExtractionResult(family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.8)
        run_pipeline(
            debtor_id="d3", invoice_id="inv3", amount_paise=10_000_00, diagnosis=diagnosis,
            channel_tag="telegram", ledger=ledger, outbound_store=store, rail=rail,
        )
        entries = list(ledger.all_entries())
        assert len(entries) == 1
        assert entries[0].actor == "ORCHESTRATOR"
        ledger.verify_chain()  # the chain is real and intact, not just "an entry exists"

    def test_a_second_run_the_same_week_increments_touches_and_can_get_refused(self, store, ledger, rail):
        """Repeated runs against the same debtor build real history through
        the ledger (Law 4) -- enough touches in 7 days should eventually hit
        a real bounds rule, not because this test rigs the context by hand."""
        diagnosis = ExtractionResult(family=Family.C, class_=DiagnosisClass.PROMISE_STATED, confidence=0.8)
        results = []
        for i in range(12):
            results.append(run_pipeline(
                debtor_id="d4", invoice_id="inv4", amount_paise=10_000_00, diagnosis=diagnosis,
                channel_tag="telegram", ledger=ledger, outbound_store=store, rail=rail,
                decision_seq=i,
            ))
        assert any(not r.bounds_passed for r in results), "expected a real touch-budget/frequency rule to eventually refuse"
        refused = next(r for r in results if not r.bounds_passed)
        assert len(refused.refusal_reasons) > 0
