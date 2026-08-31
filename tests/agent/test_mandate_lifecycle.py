"""Tests for agent.mandate.lifecycle -- the full subscription-side story
connected end to end: detect a defect, repair it for real, notify, wait
the real 24h gate, present the debit, capture. Against SimulatedRail,
which implements modify_mandate/present_debit for real (unlike
RazorpayRail on this account, which raises RailUnavailable for both --
see agent/rails/razorpay_rail.py's own docstring)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.mandate.health import MandateDefect, MandateSnapshot
from agent.mandate.lifecycle import UnrepairableDefect, detect_and_repair, notify_and_present_debit
from agent.rails.simulated import SimulatedRail
from agent.rails.types import MandateSpec

SECRET = "test-webhook-secret"

# `clock` fixture (a controllable FakeClock) comes from tests/agent/conftest.py.


@pytest.fixture
def rail(clock):
    return SimulatedRail(webhook_secret=SECRET, clock=clock)


def _make_mandate(rail, *, max_amount_paise=10_000_00, end_at="2027-01-01T00:00:00Z"):
    return rail.create_mandate(
        MandateSpec(max_amount_paise=max_amount_paise, start_at="2026-01-01T00:00:00Z", end_at=end_at)
    )


def _healthy_snapshot(*, max_amount_paise=10_000_00, end_at=datetime(2027, 1, 1, tzinfo=timezone.utc), **overrides):
    defaults = dict(max_amount_paise=max_amount_paise, end_at=end_at, status="active", afa_scheduled=True)
    defaults.update(overrides)
    return MandateSnapshot(**defaults)


class TestDetectAndRepair:
    def test_headroom_breach_is_repaired_via_a_real_modify_mandate_call(self, rail):
        mandate = _make_mandate(rail, max_amount_paise=10_000_00)
        snapshot = _healthy_snapshot(max_amount_paise=10_000_00)

        result = detect_and_repair(
            rail=rail, mandate_id=mandate.id, mandate=snapshot,
            upcoming_debit_paise=25_000_00, next_debit_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )

        assert result.defects_repaired == [MandateDefect.HEADROOM_BREACH]
        assert result.mandate_after_repair.max_amount_paise == 25_000_00
        # Confirm it's a REAL rail-side change, not just the return value.
        assert rail.fetch("mandates", mandate.id)["max_amount_paise"] == 25_000_00

    def test_expiry_before_debit_is_repaired(self, rail):
        mandate = _make_mandate(rail, end_at="2026-01-15T00:00:00Z")
        snapshot = _healthy_snapshot(end_at=datetime(2026, 1, 15, tzinfo=timezone.utc))

        result = detect_and_repair(
            rail=rail, mandate_id=mandate.id, mandate=snapshot,
            upcoming_debit_paise=5_000_00, next_debit_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )

        assert result.defects_repaired == [MandateDefect.EXPIRY_BEFORE_DEBIT]
        assert result.mandate_after_repair.end_at == datetime(2026, 2, 1, tzinfo=timezone.utc).isoformat()

    def test_both_repairable_defects_are_fixed_in_one_call(self, rail):
        mandate = _make_mandate(rail, max_amount_paise=1_000_00, end_at="2026-01-15T00:00:00Z")
        snapshot = _healthy_snapshot(max_amount_paise=1_000_00, end_at=datetime(2026, 1, 15, tzinfo=timezone.utc))

        result = detect_and_repair(
            rail=rail, mandate_id=mandate.id, mandate=snapshot,
            upcoming_debit_paise=5_000_00, next_debit_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )

        assert set(result.defects_repaired) == {MandateDefect.HEADROOM_BREACH, MandateDefect.EXPIRY_BEFORE_DEBIT}
        assert result.mandate_after_repair.max_amount_paise == 5_000_00

    def test_a_healthy_mandate_needs_no_repair_and_still_returns_its_current_state(self, rail):
        mandate = _make_mandate(rail, max_amount_paise=10_000_00)
        snapshot = _healthy_snapshot(max_amount_paise=10_000_00)

        result = detect_and_repair(
            rail=rail, mandate_id=mandate.id, mandate=snapshot,
            upcoming_debit_paise=5_000_00, next_debit_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )

        assert result.defects_detected == []
        assert result.defects_repaired == []
        assert result.mandate_after_repair.max_amount_paise == 10_000_00  # fetched via rail.fetch, unchanged

    def test_an_unrepairable_defect_raises_and_names_every_detected_defect(self, rail):
        mandate = _make_mandate(rail, max_amount_paise=10_000_00)
        snapshot = _healthy_snapshot(max_amount_paise=10_000_00, consecutive_nsf=3)  # REPEAT_NSF, not auto-repairable

        with pytest.raises(UnrepairableDefect) as exc_info:
            detect_and_repair(
                rail=rail, mandate_id=mandate.id, mandate=snapshot,
                upcoming_debit_paise=5_000_00, next_debit_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
            )
        assert any(d.defect == MandateDefect.REPEAT_NSF for d in exc_info.value.defects)

    def test_a_repairable_defect_alongside_an_unrepairable_one_still_gets_repaired_before_raising(self, rail):
        """Partial progress is applied, not discarded -- the caller still
        needs to know the unrepairable part remains, but a fixable defect
        shouldn't be left broken just because another defect also exists."""
        mandate = _make_mandate(rail, max_amount_paise=1_000_00)
        snapshot = _healthy_snapshot(max_amount_paise=1_000_00, consecutive_nsf=3)

        with pytest.raises(UnrepairableDefect):
            detect_and_repair(
                rail=rail, mandate_id=mandate.id, mandate=snapshot,
                upcoming_debit_paise=5_000_00, next_debit_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
            )
        assert rail.fetch("mandates", mandate.id)["max_amount_paise"] == 5_000_00  # the fixable part still landed


class TestNotifyAndPresentDebit:
    def test_immediate_present_after_notify_is_refused_by_the_real_24h_gate(self, rail):
        mandate = _make_mandate(rail)
        with pytest.raises(Exception, match="24h"):
            notify_and_present_debit(rail=rail, mandate_id=mandate.id, amount_paise=5_000_00, debit_datetime="2026-01-02T00:00:00Z")

    def test_a_rail_without_notify_predebit_raises_plainly_not_silently_skips(self):
        class _RailWithoutNotify:
            rail_tag = "fake"

            def present_debit(self, mandate_id, amount_paise):
                raise AssertionError("must never be reached -- the missing notice should stop this first")

        with pytest.raises(NotImplementedError, match="notify_predebit"):
            notify_and_present_debit(rail=_RailWithoutNotify(), mandate_id="m1", amount_paise=1_000_00, debit_datetime="2026-01-02T00:00:00Z")


class TestFullRealLifecycle:
    """The whole subscription story in one test: create, detect a defect
    that would have failed the next debit, repair it for real, send the
    mandatory notice, wait the real 24h gate, present the debit, capture."""

    def test_defect_detected_repaired_notified_and_captured(self, rail, clock):
        mandate = _make_mandate(rail, max_amount_paise=10_000_00)
        snapshot = _healthy_snapshot(max_amount_paise=10_000_00)

        repair = detect_and_repair(
            rail=rail, mandate_id=mandate.id, mandate=snapshot,
            upcoming_debit_paise=25_000_00, next_debit_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        assert repair.defects_repaired == [MandateDefect.HEADROOM_BREACH]

        rail.notify_predebit(mandate.id, 25_000_00, "2026-01-02T00:00:00Z", reason="scheduled subscription debit")
        clock.advance(hours=24)
        debit = rail.present_debit(mandate.id, 25_000_00)

        assert debit.status == "captured"
        assert debit.payment_id.startswith("pay_")
