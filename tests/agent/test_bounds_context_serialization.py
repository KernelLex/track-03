"""BoundsContext.to_dict()/from_dict() round-tripping -- the Auditor's
bounds-integrity job (§11.7) depends on reconstructing an exact context from
a JSON-safe snapshot stored in the ledger, not from live state."""

from __future__ import annotations

import json
from datetime import datetime, time

from agent.bounds.context import ActionCtx, BoundsContext, ConfigCtx, DebtorCtx, DecisionCtx, InvoiceCtx, MandateCtx, NotificationCtx
from agent.bounds.engine import check_bounds


def make_rich_context() -> BoundsContext:
    return BoundsContext(
        debtor=DebtorCtx(
            id="d1", state="PROMISED", touches_7d=2, opted_out_cycle=True,
            opted_out_channels=frozenset({"sms", "email"}), local_time=time(14, 30), promise_credibility=0.6,
        ),
        mandate=MandateCtx(id="sub_1", status="active", last_notification_at=datetime(2026, 5, 1, 10), afa_required=True),
        action=ActionCtx(
            type="create_mandate", channel="whatsapp", afa_reference="afa_1", human_approval_id="h1",
            carries_legal_number=True, rail_tag="simulated", is_regulatory_notice=True, presents_mandate_debit=True,
            params={"a": 1}, debtor_stated_params={"a": 1}, clamp_direction="favours_debtor",
        ),
        decision=DecisionCtx(ev_paise=4242),
        invoice=InvoiceCtx(id="inv_1", recovery_attempts=3, disputed_paise=500),
        config=ConfigCtx(promise_credibility_floor=0.4, grace_days=5, rbi_bank_rate=0.06, as_of_age_days=10),
        notification=NotificationCtx(fields=frozenset({"amount", "reason"})),
        now=datetime(2026, 6, 1, 8, 0),
        debit_paise=123456,
        post_debit_notification_queued=True,
        interest_computed_from=0.06,
        promise_date=datetime(2026, 5, 15),
    )


def test_to_dict_is_json_serializable():
    ctx = make_rich_context()
    json.dumps(ctx.to_dict())  # must not raise


def test_round_trip_reproduces_an_equal_context():
    ctx = make_rich_context()
    restored = BoundsContext.from_dict(ctx.to_dict())
    assert restored == ctx


def test_round_trip_through_actual_json_serialization():
    ctx = make_rich_context()
    restored = BoundsContext.from_dict(json.loads(json.dumps(ctx.to_dict())))
    assert restored == ctx


def test_restored_context_produces_identical_bounds_verdicts():
    """The actual claim the Auditor's bounds-integrity job depends on: a
    context reconstructed from its own snapshot must evaluate identically."""
    ctx = make_rich_context()
    restored = BoundsContext.from_dict(json.loads(json.dumps(ctx.to_dict())))

    original_result = check_bounds(ctx)
    restored_result = check_bounds(restored)

    assert [v.to_dict() for v in original_result.verdicts] == [v.to_dict() for v in restored_result.verdicts]


def test_round_trip_with_none_fields():
    ctx = BoundsContext(
        debtor=DebtorCtx(id="d1"), mandate=MandateCtx(), action=ActionCtx(),
        decision=DecisionCtx(), invoice=InvoiceCtx(), config=ConfigCtx(),
    )
    restored = BoundsContext.from_dict(json.loads(json.dumps(ctx.to_dict())))
    assert restored == ctx
