"""The differential test named in DEVDOC_v6 §13.4 and §17.7: machine rule (engine.py,
YAML-driven) vs human twin (human_twin.py, independently hand-written from the same
rules' `human:` prose), >=5,000 generated inputs, asserting agreement.

§13.4's own caveat, restated because it's load-bearing: agreement here proves
*implementation consistency* between two independently-authored readings of the
same intent — not correctness against the actual RBI/MSMED/TRAI text, since the
same person wrote both. See docs/LIMITATIONS.md.
"""

from __future__ import annotations

from datetime import datetime

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agent.bounds import human_twin
from agent.bounds.context import (
    REQUIRED_PREDEBIT_FIELDS,
    ActionCtx,
    BoundsContext,
    ConfigCtx,
    DebtorCtx,
    DecisionCtx,
    InvoiceCtx,
    MandateCtx,
    NotificationCtx,
)
from agent.bounds.engine import DEFAULT_FUNCTIONS, default_rules

CHANNELS = ["sms", "email", "whatsapp", "ivr"]
DEBTOR_STATES = [
    "HEALTHY", "AT_RISK", "DIAGNOSED", "ENGAGED", "PROMISED", "INSTRUMENTED",
    "BROKEN_PROMISE", "MANDATE_DEFECT", "REPAIRING", "DISPUTED_FROZEN",
    "STATUTORY_PENDING", "EXHAUSTED", "RECOVERED", "HUMAN_QUEUE",
]
ACTION_TYPES = [
    "reissue_artifact", "create_payment_link", "request_reconciliation", "send_reminder",
    "send_predebit_notice", "send_postdebit_notice", "check_mandate_health", "create_mandate",
    "repair_mandate", "retry_charge", "send_statutory_notice", "initiate_refund",
    "revoke_mandate", "escalate_human", "no_action",
]
MANDATE_STATUSES = [None, "created", "pending_afa", "active", "health_defect", "repairing",
                     "debit_scheduled", "notified_24h", "revoked", "expired"]

st_naive_datetime = st.datetimes(min_value=datetime(2025, 1, 1), max_value=datetime(2027, 1, 1))
st_optional_datetime = st.one_of(st.none(), st_naive_datetime)
st_params = st.one_of(st.none(), st.dictionaries(st.sampled_from(["max_amount_paise", "end_at"]),
                                                  st.integers(0, 100_000), max_size=2))

st_debtor = st.builds(
    DebtorCtx,
    id=st.just("debtor_x"),
    state=st.sampled_from(DEBTOR_STATES),
    touches_7d=st.integers(min_value=0, max_value=8),
    opted_out_cycle=st.booleans(),
    opted_out_channels=st.frozensets(st.sampled_from(CHANNELS), max_size=4),
    local_time=st.times(),
    promise_credibility=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
)

st_mandate = st.builds(
    MandateCtx,
    id=st.one_of(st.none(), st.just("sub_x")),
    status=st.sampled_from(MANDATE_STATUSES),
    last_notification_at=st_optional_datetime,
    afa_required=st.booleans(),
)

st_notification = st.builds(
    NotificationCtx,
    fields=st.frozensets(st.sampled_from(list(REQUIRED_PREDEBIT_FIELDS) + ["extra_field"]), max_size=6),
)

st_action = st.builds(
    ActionCtx,
    type=st.sampled_from(ACTION_TYPES),
    channel=st.one_of(st.none(), st.sampled_from(CHANNELS)),
    afa_reference=st.one_of(st.none(), st.just("afa_ref_x")),
    human_approval_id=st.one_of(st.none(), st.just("human_x")),
    carries_legal_number=st.booleans(),
    rail_tag=st.one_of(st.none(), st.sampled_from(["razorpay", "simulated"])),
    is_regulatory_notice=st.booleans(),
    params=st_params,
    debtor_stated_params=st_params,
    clamp_direction=st.one_of(st.none(), st.sampled_from(["favours_debtor", "favours_supplier"])),
)

st_decision = st.builds(DecisionCtx, ev_paise=st.integers(min_value=-50_000, max_value=50_000))

st_invoice = st.builds(
    InvoiceCtx,
    id=st.just("inv_x"),
    recovery_attempts=st.integers(min_value=0, max_value=8),
    disputed_paise=st.integers(min_value=0, max_value=50_000),
)

st_config = st.builds(
    ConfigCtx,
    promise_credibility_floor=st.just(0.34),
    grace_days=st.integers(min_value=1, max_value=5),
    rbi_bank_rate=st.sampled_from([0.0550, 0.09]),
    as_of_age_days=st.integers(min_value=0, max_value=200),
)

st_bounds_context = st.builds(
    BoundsContext,
    debtor=st_debtor,
    mandate=st_mandate,
    action=st_action,
    decision=st_decision,
    invoice=st_invoice,
    config=st_config,
    notification=st_notification,
    now=st_naive_datetime,
    debit_paise=st.integers(min_value=0, max_value=3_000_000),
    post_debit_notification_queued=st.booleans(),
    interest_computed_from=st.one_of(st.none(), st.sampled_from([0.0550, 0.09])),
    promise_date=st_optional_datetime,
)


@settings(max_examples=5000, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(ctx=st_bounds_context)
def test_machine_rule_and_human_twin_agree(ctx: BoundsContext) -> None:
    namespace = ctx.to_namespace()
    for rule in default_rules():
        machine_verdict = bool(rule.expr.evaluate(namespace, DEFAULT_FUNCTIONS))
        twin_fn = human_twin.REGISTRY[rule.id]
        twin_verdict = twin_fn(ctx)
        assert machine_verdict == twin_verdict, (
            f"{rule.id} disagreement: machine={machine_verdict} twin={twin_verdict} "
            f"debtor={ctx.debtor} action={ctx.action} mandate={ctx.mandate} "
            f"invoice={ctx.invoice} now={ctx.now}"
        )


def test_every_rule_in_the_register_has_a_human_twin() -> None:
    """A rule added to rules.yaml without a matching human_twin.py entry would
    silently fall out of the differential test's coverage — this fails loudly
    instead."""
    rule_ids = {rule.id for rule in default_rules()}
    twin_ids = set(human_twin.REGISTRY.keys())
    assert rule_ids == twin_ids
