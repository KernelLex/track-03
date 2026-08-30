"""ACT stage: Law 3 (bounds gate first, always), Law 6 (rail tagging), and
§9.4's outbound idempotency via claim-then-act. DEVDOC_v6 §9.4, §11.5."""

from __future__ import annotations

import pytest

from agent.act.actions import ActionType
from agent.act.executor import (
    ActionRefused,
    InFlightOrStaleDuplicate,
    OutboundActionStore,
    UnknownActionType,
    compute_idempotency_key,
    execute_action,
)
from agent.bounds.context import ActionCtx, BoundsContext, ConfigCtx, DebtorCtx, DecisionCtx, InvoiceCtx, MandateCtx
from agent.rails.simulated import SimulatedRail
from agent.rails.types import MandateDelta, MandateSpec


@pytest.fixture
def store(tmp_path):
    with OutboundActionStore(tmp_path / "outbound.db") as s:
        yield s


@pytest.fixture
def rail():
    return SimulatedRail(webhook_secret="test-secret")


def passing_bounds_context(**overrides) -> BoundsContext:
    defaults = dict(
        debtor=DebtorCtx(id="debtor_1", state="ENGAGED", touches_7d=0),
        mandate=MandateCtx(),
        action=ActionCtx(type="create_payment_link", channel="email", rail_tag="simulated"),
        decision=DecisionCtx(ev_paise=1000),
        invoice=InvoiceCtx(id="inv_1"),
        config=ConfigCtx(),
    )
    defaults.update(overrides)
    return BoundsContext(**defaults)


def test_bounds_refusal_prevents_any_rail_call(store, rail):
    ctx = passing_bounds_context(decision=DecisionCtx(ev_paise=0))  # EV_FLOOR refuses
    with pytest.raises(ActionRefused):
        execute_action(
            action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
            decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store,
            payload={"amount_paise": 10_000, "description": "test"},
        )
    assert len(rail._links) == 0  # nothing was created


def test_message_only_action_never_touches_the_rail(store, rail):
    ctx = passing_bounds_context(action=ActionCtx(type="send_reminder", channel="email", rail_tag="simulated"))
    outcome = execute_action(
        action_type=ActionType.SEND_REMINDER, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store,
        payload={"template": "gentle_nudge"},
    )
    assert outcome.external_ref is None
    assert outcome.detail["message_dispatched"] is True
    assert len(rail._links) == 0


def test_create_payment_link_creates_exactly_once_and_tags_simulated(store, rail):
    ctx = passing_bounds_context()
    outcome = execute_action(
        action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store,
        payload={"amount_paise": 25_000, "description": "Invoice 1"},
    )
    assert outcome.external_ref.startswith("plink_")
    assert outcome.rail_tag == "simulated"
    assert outcome.was_duplicate is False
    assert len(rail._links) == 1


def test_retrying_the_same_decision_seq_does_not_create_a_second_link(store, rail):
    ctx = passing_bounds_context()
    first = execute_action(
        action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=7, bounds_context=ctx, rail=rail, outbound_store=store,
        payload={"amount_paise": 25_000, "description": "Invoice 1"},
    )
    second = execute_action(
        action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=7, bounds_context=ctx, rail=rail, outbound_store=store,
        payload={"amount_paise": 25_000, "description": "Invoice 1"},
    )
    assert first.external_ref == second.external_ref
    assert second.was_duplicate is True
    assert len(rail._links) == 1  # not 2


def test_a_different_decision_seq_creates_a_genuinely_new_link(store, rail):
    ctx = passing_bounds_context()
    first = execute_action(
        action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store,
        payload={"amount_paise": 25_000, "description": "Invoice 1"},
    )
    second = execute_action(
        action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=2, bounds_context=ctx, rail=rail, outbound_store=store,
        payload={"amount_paise": 25_000, "description": "Invoice 1"},
    )
    assert first.external_ref != second.external_ref
    assert len(rail._links) == 2


def test_a_claimed_but_unfinalized_key_raises_rather_than_double_dispatching(store, rail):
    ctx = passing_bounds_context()
    key = compute_idempotency_key(
        debtor_id="debtor_1", invoice_id="inv_1", action_type=ActionType.CREATE_PAYMENT_LINK, decision_seq=3,
    )
    store.claim(key, ActionType.CREATE_PAYMENT_LINK.value)  # simulate a crashed prior dispatch: claimed, never finalized

    with pytest.raises(InFlightOrStaleDuplicate):
        execute_action(
            action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
            decision_seq=3, bounds_context=ctx, rail=rail, outbound_store=store,
            payload={"amount_paise": 25_000, "description": "Invoice 1"},
        )
    assert len(rail._links) == 0  # the rail was never called


def test_reissue_artifact_creates_an_invoice(store, rail):
    ctx = passing_bounds_context(action=ActionCtx(type="reissue_artifact", rail_tag="simulated"))
    outcome = execute_action(
        action_type=ActionType.REISSUE_ARTIFACT, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store,
        payload={"amount_paise": 40_000, "description": "Corrected GSTIN"},
    )
    assert outcome.external_ref.startswith("inv_")


def test_create_mandate_dispatches_with_pending_afa_status(store, rail):
    ctx = passing_bounds_context(action=ActionCtx(
        type="create_mandate", rail_tag="simulated",
        params={"max_amount_paise": 10_000}, debtor_stated_params={"max_amount_paise": 10_000},
    ))
    outcome = execute_action(
        action_type=ActionType.CREATE_MANDATE, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store,
        payload={"max_amount_paise": 10_000, "start_at": "2026-01-01T00:00:00Z", "end_at": "2027-01-01T00:00:00Z"},
    )
    assert outcome.external_ref.startswith("sub_")
    assert outcome.detail["status"] == "pending_afa"


def test_initiate_refund_is_human_gated_and_dispatches_when_approved(store, rail):
    # First capture a real payment to refund.
    from agent.rails.types import LinkSpec
    link = rail.create_payment_link(LinkSpec(amount_paise=15_000, description="x"))
    rail.simulate_link_paid(link.id)
    import json
    payment_id = json.loads(rail.emitted_webhooks[-1].body)["payload"]["payment"]["entity"]["id"]

    ctx = passing_bounds_context(action=ActionCtx(
        type="initiate_refund", rail_tag="simulated", human_approval_id="approval_1",
    ))
    outcome = execute_action(
        action_type=ActionType.INITIATE_REFUND, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store,
        payload={"payment_id": payment_id, "reason": "erroneous debit"},
    )
    assert outcome.external_ref.startswith("rfnd_")


def test_initiate_refund_without_human_approval_is_refused_by_bounds(store, rail):
    ctx = passing_bounds_context(action=ActionCtx(
        type="initiate_refund", rail_tag="simulated", human_approval_id=None,
    ))
    with pytest.raises(ActionRefused):
        execute_action(
            action_type=ActionType.INITIATE_REFUND, debtor_id="debtor_1", invoice_id="inv_1",
            decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store,
            payload={"payment_id": "pay_whatever", "reason": "test"},
        )


def test_revoke_mandate_without_approval_or_optout_is_refused_by_bounds(store, rail):
    ctx = passing_bounds_context(action=ActionCtx(
        type="revoke_mandate", rail_tag="simulated", human_approval_id=None,
    ))
    with pytest.raises(ActionRefused):
        execute_action(
            action_type=ActionType.REVOKE_MANDATE, debtor_id="debtor_1", invoice_id="inv_1",
            decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store,
            payload={"mandate_id": "sub_whatever"},
        )


def test_revoke_mandate_on_debtor_optout_proceeds_autonomously(store, rail):
    """§11.6: refusing to reverse a mandate on debtor opt-out is itself a
    violation, so this specific case is the one exception to the human gate."""
    mandate = rail.create_mandate(MandateSpec(
        max_amount_paise=10_000, start_at="2026-01-01T00:00:00Z", end_at="2027-01-01T00:00:00Z"
    ))
    ctx = passing_bounds_context(
        debtor=DebtorCtx(id="debtor_1", state="ENGAGED", opted_out_cycle=True),
        action=ActionCtx(type="revoke_mandate", rail_tag="simulated", human_approval_id=None),
    )
    outcome = execute_action(
        action_type=ActionType.REVOKE_MANDATE, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store,
        payload={"mandate_id": mandate.id},
    )
    assert outcome.detail["status"] == "revoked"


def test_repair_mandate_dispatches_a_modify_call(store, rail):
    mandate = rail.create_mandate(MandateSpec(
        max_amount_paise=10_000, start_at="2026-01-01T00:00:00Z", end_at="2027-01-01T00:00:00Z"
    ))
    ctx = passing_bounds_context(action=ActionCtx(type="repair_mandate", rail_tag="simulated"))
    outcome = execute_action(
        action_type=ActionType.REPAIR_MANDATE, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store,
        payload={"mandate_id": mandate.id, "delta": MandateDelta(max_amount_paise=20_000)},
    )
    assert outcome.detail["max_amount_paise"] == 20_000


def test_unknown_action_type_raises_rather_than_silently_no_opping(store, rail):
    class FakeActionType:
        value = "not_a_real_action"

    ctx = passing_bounds_context()
    with pytest.raises(UnknownActionType):
        execute_action(
            action_type=FakeActionType(), debtor_id="debtor_1", invoice_id="inv_1",  # type: ignore[arg-type]
            decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, payload={},
        )


def test_idempotency_key_is_stable_for_identical_inputs_and_differs_for_any_change():
    key_a = compute_idempotency_key(debtor_id="d1", invoice_id="inv1", action_type=ActionType.CREATE_PAYMENT_LINK, decision_seq=1)
    key_b = compute_idempotency_key(debtor_id="d1", invoice_id="inv1", action_type=ActionType.CREATE_PAYMENT_LINK, decision_seq=1)
    key_c = compute_idempotency_key(debtor_id="d1", invoice_id="inv1", action_type=ActionType.CREATE_PAYMENT_LINK, decision_seq=2)
    assert key_a == key_b
    assert key_a != key_c
