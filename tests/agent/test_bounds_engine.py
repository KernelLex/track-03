"""check_bounds() example-based behaviour: each rule's PASS/REFUSE cases, plus the
two DoS-exploit fixes from §24.2 (promise credibility scaling, channel exhaustion)."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from agent.bounds.context import ActionCtx, BoundsContext, ConfigCtx, DebtorCtx, DecisionCtx, InvoiceCtx, MandateCtx, NotificationCtx
from agent.bounds.engine import check_bounds


def base_context(**overrides) -> BoundsContext:
    defaults = dict(
        debtor=DebtorCtx(id="debtor_1", state="ENGAGED"),
        mandate=MandateCtx(),
        action=ActionCtx(type="send_reminder", channel="email", rail_tag="simulated"),
        decision=DecisionCtx(ev_paise=1000),
        invoice=InvoiceCtx(id="inv_1"),
        config=ConfigCtx(),
        notification=NotificationCtx(),
        now=datetime(2026, 6, 15, 12, 0),
    )
    defaults.update(overrides)
    return BoundsContext(**defaults)


def verdict_for(result, rule_id: str) -> str:
    return next(v.verdict for v in result.verdicts if v.rule_id == rule_id)


def test_a_well_formed_action_passes_every_rule():
    result = check_bounds(base_context())
    assert result.passed, result.refusals


def test_touch_budget_refuses_a_fourth_touch_but_exempts_regulatory_notices():
    ctx = base_context(debtor=DebtorCtx(id="d", state="ENGAGED", touches_7d=3))
    assert verdict_for(check_bounds(ctx), "TOUCH_BUDGET") == "REFUSE"

    ctx_notice = base_context(
        debtor=DebtorCtx(id="d", state="ENGAGED", touches_7d=3),
        action=ActionCtx(type="send_predebit_notice", is_regulatory_notice=True, rail_tag="simulated"),
    )
    assert verdict_for(check_bounds(ctx_notice), "TOUCH_BUDGET") == "PASS"


def test_ev_floor_refuses_non_positive_ev():
    ctx = base_context(decision=DecisionCtx(ev_paise=0))
    assert verdict_for(check_bounds(ctx), "EV_FLOOR") == "REFUSE"


def test_attempt_ceiling_refuses_at_six():
    ctx = base_context(invoice=InvoiceCtx(id="inv", recovery_attempts=6))
    assert verdict_for(check_bounds(ctx), "ATTEMPT_CEILING") == "REFUSE"


def test_exhausted_debtor_refuses_everything_downstream_of_that_rule():
    ctx = base_context(debtor=DebtorCtx(id="d", state="EXHAUSTED"))
    assert verdict_for(check_bounds(ctx), "EXHAUSTED") == "REFUSE"


def test_rail_disclosure_refuses_an_untagged_action():
    ctx = base_context(action=ActionCtx(type="send_reminder", channel="email", rail_tag=None))
    assert verdict_for(check_bounds(ctx), "RAIL_DISCLOSURE") == "REFUSE"


def test_no_mandate_on_disputed_invoice():
    ctx = base_context(
        action=ActionCtx(type="create_mandate", rail_tag="simulated", params={}, debtor_stated_params={}),
        invoice=InvoiceCtx(id="inv", disputed_paise=500),
    )
    assert verdict_for(check_bounds(ctx), "NO_MANDATE_ON_DISPUTE") == "REFUSE"


def test_mandate_param_clamp_allows_debtor_stated_params_autonomously():
    ctx = base_context(
        action=ActionCtx(
            type="create_mandate", rail_tag="simulated",
            params={"max_amount_paise": 10_000}, debtor_stated_params={"max_amount_paise": 10_000},
        ),
    )
    assert verdict_for(check_bounds(ctx), "MANDATE_PARAM_CLAMP") == "PASS"


def test_mandate_param_clamp_refuses_an_unfavourable_change_without_human_approval():
    ctx = base_context(
        action=ActionCtx(
            type="create_mandate", rail_tag="simulated",
            params={"max_amount_paise": 20_000}, debtor_stated_params={"max_amount_paise": 10_000},
            clamp_direction=None, human_approval_id=None,
        ),
    )
    assert verdict_for(check_bounds(ctx), "MANDATE_PARAM_CLAMP") == "REFUSE"


def test_statutory_human_gate_refuses_an_unapproved_legal_claim():
    ctx = base_context(action=ActionCtx(type="send_statutory_notice", carries_legal_number=True, rail_tag="simulated"))
    assert verdict_for(check_bounds(ctx), "STATUTORY_HUMAN_GATE") == "REFUSE"


def test_statutory_human_gate_passes_with_approval():
    ctx = base_context(
        action=ActionCtx(
            type="send_statutory_notice", carries_legal_number=True,
            human_approval_id="approval_1", rail_tag="simulated",
        )
    )
    assert verdict_for(check_bounds(ctx), "STATUTORY_HUMAN_GATE") == "PASS"


def test_dispute_freeze_blocks_non_escalation_actions():
    ctx = base_context(
        debtor=DebtorCtx(id="d", state="DISPUTED_FROZEN"),
        action=ActionCtx(type="send_reminder", channel="email", rail_tag="simulated"),
    )
    assert verdict_for(check_bounds(ctx), "DISPUTE_FREEZE") == "REFUSE"


def test_dispute_freeze_allows_escalate_human_and_no_action():
    for action_type in ("escalate_human", "no_action"):
        ctx = base_context(
            debtor=DebtorCtx(id="d", state="DISPUTED_FROZEN"),
            action=ActionCtx(type=action_type, rail_tag="simulated"),
            decision=DecisionCtx(ev_paise=1000 if action_type != "no_action" else 0),
        )
        assert verdict_for(check_bounds(ctx), "DISPUTE_FREEZE") == "PASS"


# ---- §24.2 fixes ----


def test_promise_cooldown_scales_with_credibility_not_a_hard_cliff():
    """A debtor with perfect credibility (1.0) gets the full grace_days cooldown;
    a debtor who has broken every promise (0.0) gets none — the exploit §24.2
    found (promise repeatedly, cooldown resets forever) is closed by credibility
    trending toward 0, not by a hardcoded cap on promise count."""
    promised_at = datetime(2026, 6, 1, 0, 0)
    just_after_promise = datetime(2026, 6, 1, 1, 0)  # 1 hour later — inside any real cooldown

    perfect_credibility = base_context(
        debtor=DebtorCtx(id="d", state="PROMISED", promise_credibility=1.0),
        promise_date=promised_at, now=just_after_promise,
        config=ConfigCtx(grace_days=3),
    )
    assert verdict_for(check_bounds(perfect_credibility), "PROMISE_COOLDOWN") == "REFUSE"  # still in cooldown

    zero_credibility = base_context(
        debtor=DebtorCtx(id="d", state="PROMISED", promise_credibility=0.0),
        promise_date=promised_at, now=just_after_promise,
        config=ConfigCtx(grace_days=3),
    )
    assert verdict_for(check_bounds(zero_credibility), "PROMISE_COOLDOWN") == "PASS"  # no cooldown left to buy


def test_channel_exhaustion_routes_to_human_instead_of_going_silent():
    """The CHANNEL_HOPPER exploit (§24.3): opt out of every channel one at a time.
    Once none remain, ordinary contact actions are refused, but escalate_human
    and regulatory notices are still allowed — the case surfaces, it doesn't stall."""
    all_opted_out = base_context(
        debtor=DebtorCtx(id="d", state="ENGAGED", opted_out_channels=frozenset({"sms", "email", "whatsapp", "ivr"})),
        action=ActionCtx(type="send_reminder", channel="email", rail_tag="simulated"),
    )
    assert verdict_for(check_bounds(all_opted_out), "CHANNEL_EXHAUSTION") == "REFUSE"

    escalation = base_context(
        debtor=DebtorCtx(id="d", state="ENGAGED", opted_out_channels=frozenset({"sms", "email", "whatsapp", "ivr"})),
        action=ActionCtx(type="escalate_human", rail_tag="simulated"),
    )
    assert verdict_for(check_bounds(escalation), "CHANNEL_EXHAUSTION") == "PASS"

    statutory_notice = base_context(
        debtor=DebtorCtx(id="d", state="ENGAGED", opted_out_channels=frozenset({"sms", "email", "whatsapp", "ivr"})),
        action=ActionCtx(type="send_statutory_notice", is_regulatory_notice=True,
                          carries_legal_number=True, human_approval_id="a1", rail_tag="simulated"),
    )
    assert verdict_for(check_bounds(statutory_notice), "CHANNEL_EXHAUSTION") == "PASS"


def test_predebit_24h_only_applies_to_actions_that_present_a_mandate_debit():
    """Unguarded, this rule would block every action in the system (nothing but a
    mandate debit ever sets mandate.last_notification_at) — the bug found while
    implementing §13.1. A plain reminder must pass regardless of mandate state."""
    ctx = base_context(action=ActionCtx(type="send_reminder", channel="email", rail_tag="simulated",
                                         presents_mandate_debit=False))
    assert verdict_for(check_bounds(ctx), "RBI_EMANDATE_PREDEBIT_24H") == "PASS"


def test_predebit_24h_refuses_without_full_notification_fields():
    ctx = base_context(
        action=ActionCtx(type="retry_charge", rail_tag="simulated", presents_mandate_debit=True),
        mandate=MandateCtx(status="notified_24h", last_notification_at=datetime(2026, 6, 1)),
        notification=NotificationCtx(fields=frozenset({"amount"})),  # missing most required fields
        now=datetime(2026, 6, 3),
    )
    assert verdict_for(check_bounds(ctx), "RBI_EMANDATE_PREDEBIT_24H") == "REFUSE"


def test_predebit_24h_passes_with_full_fields_and_elapsed_time():
    ctx = base_context(
        action=ActionCtx(type="retry_charge", rail_tag="simulated", presents_mandate_debit=True),
        mandate=MandateCtx(status="notified_24h", last_notification_at=datetime(2026, 6, 1)),
        notification=NotificationCtx(
            fields=frozenset({"merchant_name", "amount", "debit_datetime", "mandate_ref", "reason"})
        ),
        now=datetime(2026, 6, 3),
    )
    assert verdict_for(check_bounds(ctx), "RBI_EMANDATE_PREDEBIT_24H") == "PASS"


def test_afa_ceiling_requires_afa_reference_above_15000_rupees():
    ctx = base_context(debit_paise=20_00_000)  # Rs 20,000
    assert verdict_for(check_bounds(ctx), "RBI_EMANDATE_AFA_CEILING") == "REFUSE"

    ctx_with_afa = base_context(
        debit_paise=20_00_000,
        action=ActionCtx(type="retry_charge", afa_reference="afa_1", rail_tag="simulated"),
    )
    assert verdict_for(check_bounds(ctx_with_afa), "RBI_EMANDATE_AFA_CEILING") == "PASS"


def test_fpc_hours_blocks_contact_outside_8_to_19():
    ctx = base_context(debtor=DebtorCtx(id="d", state="ENGAGED", local_time=time(21, 0)))
    assert verdict_for(check_bounds(ctx), "RBI_FPC_HOURS") == "REFUSE"


def test_trai_dnd_blocks_only_the_opted_out_channel():
    ctx = base_context(
        debtor=DebtorCtx(id="d", state="ENGAGED", opted_out_channels=frozenset({"email"})),
        action=ActionCtx(type="send_reminder", channel="email", rail_tag="simulated"),
    )
    assert verdict_for(check_bounds(ctx), "TRAI_DND") == "REFUSE"

    ctx_other_channel = base_context(
        debtor=DebtorCtx(id="d", state="ENGAGED", opted_out_channels=frozenset({"email"})),
        action=ActionCtx(type="send_reminder", channel="sms", rail_tag="simulated"),
    )
    assert verdict_for(check_bounds(ctx_other_channel), "TRAI_DND") == "PASS"
