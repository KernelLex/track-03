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
from agent.ledger.store import Ledger
from agent.rails.simulated import SimulatedRail
from agent.rails.types import MandateDelta, MandateSpec


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


def test_bounds_refusal_prevents_any_rail_call(store, rail, ledger):
    ctx = passing_bounds_context(decision=DecisionCtx(ev_paise=0))  # EV_FLOOR refuses
    with pytest.raises(ActionRefused):
        execute_action(
            action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
            decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
            payload={"amount_paise": 10_000, "description": "test"},
        )
    assert len(rail._links) == 0  # nothing was created


def test_a_refusal_still_writes_a_ledger_entry(store, rail, ledger):
    """Law 4: every proposed action passes through the ledger, including
    ones check_bounds refuses -- this is what §13.3's refusal log would be
    derived from."""
    ctx = passing_bounds_context(decision=DecisionCtx(ev_paise=0))
    with pytest.raises(ActionRefused):
        execute_action(
            action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
            decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
            payload={"amount_paise": 10_000, "description": "test"},
        )
    entries = list(ledger.all_entries())
    assert len(entries) == 1
    assert entries[0].action is None
    assert any(v["verdict"] == "REFUSE" and v["rule_id"] == "EV_FLOOR" for v in entries[0].bounds_checks)


def test_a_successful_dispatch_writes_a_ledger_entry_with_a_bounds_context_snapshot(store, rail, ledger):
    ctx = passing_bounds_context()
    execute_action(
        action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
        payload={"amount_paise": 25_000, "description": "Invoice 1"},
    )
    [entry] = list(ledger.all_entries())
    assert entry.action["type"] == "create_payment_link"
    assert entry.action["payload"]["amount_paise"] == 25_000
    assert entry.action["bounds_context_snapshot"]["debtor"]["id"] == "debtor_1"
    assert entry.outcome["was_duplicate"] is False
    assert entry.rail_tag == "simulated"
    assert all(v["verdict"] == "PASS" for v in entry.bounds_checks)


def test_a_deduped_retry_still_writes_its_own_ledger_entry_marked_duplicate(store, rail, ledger):
    ctx = passing_bounds_context()
    for _ in range(2):
        execute_action(
            action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
            decision_seq=9, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
            payload={"amount_paise": 25_000, "description": "Invoice 1"},
        )
    entries = list(ledger.all_entries())
    assert len(entries) == 2
    assert entries[0].outcome["was_duplicate"] is False
    assert entries[1].outcome["was_duplicate"] is True
    assert entries[0].action["payload"]["amount_paise"] == entries[1].action["payload"]["amount_paise"]


def test_message_only_action_never_touches_the_rail(store, rail, ledger):
    ctx = passing_bounds_context(action=ActionCtx(type="send_reminder", channel="email", rail_tag="simulated"))
    outcome = execute_action(
        action_type=ActionType.SEND_REMINDER, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
        payload={"template": "gentle_nudge"},
    )
    assert outcome.external_ref is None
    assert outcome.detail["message_dispatched"] is True
    assert len(rail._links) == 0


def test_message_only_action_with_a_channel_and_recipient_sends_for_real(store, rail, ledger):
    """Passing a MessageChannel plus a payload carrying to/text turns the old
    stub into a real send, through the exact same bounds check and
    idempotency claim as any other action -- see agent.notify.protocol."""
    from agent.notify.simulated import SimulatedChannel

    channel = SimulatedChannel()
    ctx = passing_bounds_context(action=ActionCtx(type="send_reminder", channel="telegram", rail_tag="simulated"))
    outcome = execute_action(
        action_type=ActionType.SEND_REMINDER, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
        payload={"to": "123456789", "text": "Invoice 1 is 22 days overdue."},
        channel=channel,
    )
    assert outcome.detail["channel_status"] == "sent"
    assert outcome.external_ref == "sim-1"
    assert channel.sent == [{"to": "123456789", "text": "Invoice 1 is 22 days overdue."}]


def test_message_only_action_without_to_or_text_falls_back_to_stub_even_with_a_channel(store, rail, ledger):
    """A channel alone isn't enough -- the payload has to actually carry a
    recipient and text, or this stays the old no-op-but-logged behaviour
    rather than guessing at defaults."""
    from agent.notify.simulated import SimulatedChannel

    channel = SimulatedChannel()
    ctx = passing_bounds_context(action=ActionCtx(type="send_reminder", channel="email", rail_tag="simulated"))
    outcome = execute_action(
        action_type=ActionType.SEND_REMINDER, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
        payload={"template": "gentle_nudge"},
        channel=channel,
    )
    assert outcome.detail["message_dispatched"] is True
    assert channel.sent == []


def test_create_payment_link_creates_exactly_once_and_tags_simulated(store, rail, ledger):
    ctx = passing_bounds_context()
    outcome = execute_action(
        action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
        payload={"amount_paise": 25_000, "description": "Invoice 1"},
    )
    assert outcome.external_ref.startswith("plink_")
    assert outcome.rail_tag == "simulated"
    assert outcome.was_duplicate is False
    assert len(rail._links) == 1


def test_retrying_the_same_decision_seq_does_not_create_a_second_link(store, rail, ledger):
    ctx = passing_bounds_context()
    first = execute_action(
        action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=7, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
        payload={"amount_paise": 25_000, "description": "Invoice 1"},
    )
    second = execute_action(
        action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=7, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
        payload={"amount_paise": 25_000, "description": "Invoice 1"},
    )
    assert first.external_ref == second.external_ref
    assert second.was_duplicate is True
    assert len(rail._links) == 1  # not 2


def test_a_different_decision_seq_creates_a_genuinely_new_link(store, rail, ledger):
    ctx = passing_bounds_context()
    first = execute_action(
        action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
        payload={"amount_paise": 25_000, "description": "Invoice 1"},
    )
    second = execute_action(
        action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=2, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
        payload={"amount_paise": 25_000, "description": "Invoice 1"},
    )
    assert first.external_ref != second.external_ref
    assert len(rail._links) == 2


def test_a_claimed_but_unfinalized_key_raises_rather_than_double_dispatching(store, rail, ledger):
    ctx = passing_bounds_context()
    key = compute_idempotency_key(
        debtor_id="debtor_1", invoice_id="inv_1", action_type=ActionType.CREATE_PAYMENT_LINK, decision_seq=3,
    )
    store.claim(key, ActionType.CREATE_PAYMENT_LINK.value)  # simulate a crashed prior dispatch: claimed, never finalized

    with pytest.raises(InFlightOrStaleDuplicate):
        execute_action(
            action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
            decision_seq=3, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
            payload={"amount_paise": 25_000, "description": "Invoice 1"},
        )
    assert len(rail._links) == 0  # the rail was never called


def test_reissue_artifact_creates_an_invoice(store, rail, ledger):
    ctx = passing_bounds_context(action=ActionCtx(type="reissue_artifact", rail_tag="simulated"))
    outcome = execute_action(
        action_type=ActionType.REISSUE_ARTIFACT, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
        payload={"amount_paise": 40_000, "description": "Corrected GSTIN"},
    )
    assert outcome.external_ref.startswith("inv_")


def test_create_mandate_dispatches_with_pending_afa_status(store, rail, ledger):
    ctx = passing_bounds_context(action=ActionCtx(
        type="create_mandate", rail_tag="simulated",
        params={"max_amount_paise": 10_000}, debtor_stated_params={"max_amount_paise": 10_000},
    ))
    outcome = execute_action(
        action_type=ActionType.CREATE_MANDATE, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
        payload={"max_amount_paise": 10_000, "start_at": "2026-01-01T00:00:00Z", "end_at": "2027-01-01T00:00:00Z"},
    )
    assert outcome.external_ref.startswith("sub_")
    assert outcome.detail["status"] == "pending_afa"


def test_initiate_refund_is_human_gated_and_dispatches_when_approved(store, rail, ledger):
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
        decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
        payload={"payment_id": payment_id, "reason": "erroneous debit"},
    )
    assert outcome.external_ref.startswith("rfnd_")


def test_initiate_refund_without_human_approval_is_refused_by_bounds(store, rail, ledger):
    ctx = passing_bounds_context(action=ActionCtx(
        type="initiate_refund", rail_tag="simulated", human_approval_id=None,
    ))
    with pytest.raises(ActionRefused):
        execute_action(
            action_type=ActionType.INITIATE_REFUND, debtor_id="debtor_1", invoice_id="inv_1",
            decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
            payload={"payment_id": "pay_whatever", "reason": "test"},
        )


def test_revoke_mandate_without_approval_or_optout_is_refused_by_bounds(store, rail, ledger):
    ctx = passing_bounds_context(action=ActionCtx(
        type="revoke_mandate", rail_tag="simulated", human_approval_id=None,
    ))
    with pytest.raises(ActionRefused):
        execute_action(
            action_type=ActionType.REVOKE_MANDATE, debtor_id="debtor_1", invoice_id="inv_1",
            decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
            payload={"mandate_id": "sub_whatever"},
        )


def test_revoke_mandate_on_debtor_optout_proceeds_autonomously(store, rail, ledger):
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
        decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
        payload={"mandate_id": mandate.id},
    )
    assert outcome.detail["status"] == "revoked"


def test_repair_mandate_dispatches_a_modify_call(store, rail, ledger):
    mandate = rail.create_mandate(MandateSpec(
        max_amount_paise=10_000, start_at="2026-01-01T00:00:00Z", end_at="2027-01-01T00:00:00Z"
    ))
    ctx = passing_bounds_context(action=ActionCtx(type="repair_mandate", rail_tag="simulated"))
    outcome = execute_action(
        action_type=ActionType.REPAIR_MANDATE, debtor_id="debtor_1", invoice_id="inv_1",
        decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
        payload={"mandate_id": mandate.id, "delta": MandateDelta(max_amount_paise=20_000)},
    )
    assert outcome.detail["max_amount_paise"] == 20_000


def test_unknown_action_type_raises_rather_than_silently_no_opping(store, rail, ledger):
    class FakeActionType:
        value = "not_a_real_action"

    ctx = passing_bounds_context()
    with pytest.raises(UnknownActionType):
        execute_action(
            action_type=FakeActionType(), debtor_id="debtor_1", invoice_id="inv_1",  # type: ignore[arg-type]
            decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger, payload={},
        )


def test_idempotency_key_is_stable_for_identical_inputs_and_differs_for_any_change():
    key_a = compute_idempotency_key(debtor_id="d1", invoice_id="inv1", action_type=ActionType.CREATE_PAYMENT_LINK, decision_seq=1)
    key_b = compute_idempotency_key(debtor_id="d1", invoice_id="inv1", action_type=ActionType.CREATE_PAYMENT_LINK, decision_seq=1)
    key_c = compute_idempotency_key(debtor_id="d1", invoice_id="inv1", action_type=ActionType.CREATE_PAYMENT_LINK, decision_seq=2)
    assert key_a == key_b
    assert key_a != key_c


class TestDryRun:
    """A shadow mode on ACT: bounds runs for real, nothing else does."""

    def test_dry_run_never_touches_the_rail(self, store, rail, ledger):
        ctx = passing_bounds_context()
        outcome = execute_action(
            action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
            decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
            payload={"amount_paise": 25_000, "description": "test"}, dry_run=True,
        )
        assert len(rail._links) == 0  # nothing was created on the rail
        assert outcome.external_ref is None
        assert outcome.dry_run is True
        assert outcome.detail["would_dispatch"] == "create_payment_link"

    def test_dry_run_still_enforces_bounds(self, store, rail, ledger):
        """A dry run proves the pipeline's judgment, not a weaker version of
        it -- an action bounds would refuse for real is still refused."""
        ctx = passing_bounds_context(decision=DecisionCtx(ev_paise=0))  # EV_FLOOR refuses
        with pytest.raises(ActionRefused):
            execute_action(
                action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
                decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
                payload={"amount_paise": 10_000, "description": "test"}, dry_run=True,
            )
        assert len(rail._links) == 0

    def test_dry_run_writes_a_ledger_entry_tagged_dry_run(self, store, rail, ledger):
        ctx = passing_bounds_context()
        execute_action(
            action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
            decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
            payload={"amount_paise": 25_000, "description": "test"}, dry_run=True,
        )
        [entry] = list(ledger.all_entries())
        assert entry.outcome["dry_run"] is True
        assert entry.outcome["external_ref"] is None
        assert entry.rail_tag is None  # no rail was actually consulted

    def test_dry_run_never_claims_the_idempotency_key(self, store, rail, ledger):
        """The whole point: a dry run must never block, or be confused with,
        a later real dispatch of the identical action."""
        ctx = passing_bounds_context()
        key = compute_idempotency_key(debtor_id="debtor_1", invoice_id="inv_1", action_type=ActionType.CREATE_PAYMENT_LINK, decision_seq=1)

        execute_action(
            action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
            decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
            payload={"amount_paise": 25_000, "description": "test"}, dry_run=True,
        )
        assert store.get(key) is None  # nothing claimed

        real_outcome = execute_action(
            action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
            decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
            payload={"amount_paise": 25_000, "description": "test"},
        )
        assert real_outcome.dry_run is False
        assert real_outcome.was_duplicate is False  # the dry run didn't count as a prior real dispatch
        assert len(rail._links) == 1  # exactly one real link, from the real call only

    def test_repeated_dry_runs_of_the_same_action_never_collide(self, store, rail, ledger):
        ctx = passing_bounds_context()
        for _ in range(3):
            outcome = execute_action(
                action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="debtor_1", invoice_id="inv_1",
                decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
                payload={"amount_paise": 25_000, "description": "test"}, dry_run=True,
            )
            assert outcome.dry_run is True
            assert outcome.was_duplicate is False
        assert len(rail._links) == 0
        assert len(list(ledger.all_entries())) == 3  # each dry run still gets its own real ledger entry

    def test_dry_run_message_only_action_does_not_send(self, store, rail, ledger):
        from agent.notify.simulated import SimulatedChannel

        channel = SimulatedChannel()
        ctx = passing_bounds_context(action=ActionCtx(type="send_reminder", channel="telegram", rail_tag="simulated"))
        outcome = execute_action(
            action_type=ActionType.SEND_REMINDER, debtor_id="debtor_1", invoice_id="inv_1",
            decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store, ledger=ledger,
            payload={"to": "999", "text": "hello"}, channel=channel, dry_run=True,
        )
        assert channel.sent == []  # nothing actually sent
        assert outcome.dry_run is True
