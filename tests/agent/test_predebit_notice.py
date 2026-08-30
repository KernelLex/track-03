"""§12.4's pre-debit notification builder, including the integration claim that
matters most: its output actually satisfies RBI_EMANDATE_PREDEBIT_24H when
fed into check_bounds() for real, not just structurally similar to it."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from agent.bounds.context import ActionCtx, BoundsContext, ConfigCtx, DebtorCtx, DecisionCtx, InvoiceCtx, MandateCtx, NotificationCtx
from agent.bounds.engine import check_bounds
from agent.mandate.predebit_notice import MissingAfaLink, PredebitNotificationContext, build_predebit_notification


def base_ctx(**overrides) -> PredebitNotificationContext:
    defaults = dict(
        merchant_name="Acme Supplies", amount_paise=10_000, debit_datetime=datetime(2026, 6, 2),
        mandate_ref="sub_abc123", reason="scheduled installment 2 of 6",
        opt_out_url="https://example.com/optout", reschedule_url="https://example.com/reschedule",
        pay_early_url="https://example.com/pay-early",
    )
    defaults.update(overrides)
    return PredebitNotificationContext(**defaults)


def test_all_five_mandatory_fields_are_present():
    notice = build_predebit_notification(base_ctx())
    assert set(notice.fields) == {"merchant_name", "amount", "debit_datetime", "mandate_ref", "reason"}


def test_opt_out_is_always_in_the_message_body():
    notice = build_predebit_notification(base_ctx())
    assert any("optout" in line for line in notice.body_lines)


def test_reschedule_and_pay_early_links_are_present():
    notice = build_predebit_notification(base_ctx())
    body = " ".join(notice.body_lines)
    assert "reschedule" in body
    assert "pay-early" in body


def test_repeat_nsf_nudge_appears_at_the_threshold():
    notice = build_predebit_notification(base_ctx(consecutive_nsf=2))
    assert any("did not go through" in line for line in notice.body_lines)


def test_no_nsf_nudge_below_the_threshold():
    notice = build_predebit_notification(base_ctx(consecutive_nsf=1))
    assert not any("did not go through" in line for line in notice.body_lines)


def test_amount_above_ceiling_requires_an_afa_url():
    with pytest.raises(MissingAfaLink):
        build_predebit_notification(base_ctx(amount_paise=20_000_00, afa_url=None))


def test_amount_above_ceiling_with_afa_url_includes_it_in_the_body():
    notice = build_predebit_notification(base_ctx(amount_paise=20_000_00, afa_url="https://example.com/afa"))
    assert any("afa" in line for line in notice.body_lines)


def test_amount_at_or_below_ceiling_never_requires_afa():
    notice = build_predebit_notification(base_ctx(amount_paise=15_00_00, afa_url=None))  # exactly Rs 15,000
    assert not any("authentication" in line for line in notice.body_lines)


# ---- Integration: this builder's output actually satisfies the bounds gate ----


def test_a_freshly_built_notification_satisfies_the_predebit_24h_bound():
    now = datetime(2026, 6, 3)
    debit_at = now + timedelta(days=1)
    notice = build_predebit_notification(base_ctx(debit_datetime=debit_at))

    ctx = BoundsContext(
        debtor=DebtorCtx(id="d1", state="ENGAGED"),
        mandate=MandateCtx(status="notified_24h", last_notification_at=now - timedelta(hours=25)),
        action=ActionCtx(type="retry_charge", rail_tag="simulated", presents_mandate_debit=True),
        decision=DecisionCtx(ev_paise=1000),
        invoice=InvoiceCtx(id="inv1"),
        config=ConfigCtx(),
        notification=NotificationCtx(fields=frozenset(notice.fields.keys())),
        now=now,
    )
    result = check_bounds(ctx)
    refusal_ids = {v.rule_id for v in result.refusals}
    assert "RBI_EMANDATE_PREDEBIT_24H" not in refusal_ids
