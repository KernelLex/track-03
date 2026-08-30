"""Every defect row of DEVDOC_v6 §12.3's table, each triggered independently and
in combination — pure functions over object shape, no rail needed (§5.2)."""

from __future__ import annotations

from datetime import datetime

from agent.mandate.health import (
    DEFAULT_ISSUER_FAILURE_RATE_THRESHOLD,
    HealthCheckInput,
    MandateDefect,
    MandateSnapshot,
    check_mandate_health,
)

HEALTHY_MANDATE = MandateSnapshot(
    max_amount_paise=50_000_00, end_at=datetime(2027, 1, 1), status="active",
    afa_scheduled=True, consecutive_nsf=0, issuer_failure_rate=0.02,
)


def healthy_input(**overrides) -> HealthCheckInput:
    defaults = dict(
        mandate=HEALTHY_MANDATE,
        upcoming_debit_paise=10_000_00,
        next_debit_date=datetime(2026, 6, 1),
        cycle_was_attempted=True,
    )
    defaults.update(overrides)
    return HealthCheckInput(**defaults)


def test_a_fully_healthy_mandate_has_no_defects():
    assert check_mandate_health(healthy_input()) == []


def test_headroom_breach_when_max_amount_below_upcoming_debit():
    inp = healthy_input(mandate=MandateSnapshot(
        max_amount_paise=5_000_00, end_at=datetime(2027, 1, 1), status="active",
    ), upcoming_debit_paise=10_000_00)
    defects = {d.defect for d in check_mandate_health(inp)}
    assert MandateDefect.HEADROOM_BREACH in defects


def test_expiry_before_debit():
    inp = healthy_input(mandate=MandateSnapshot(
        max_amount_paise=50_000_00, end_at=datetime(2026, 5, 1), status="active",
    ), next_debit_date=datetime(2026, 6, 1))
    defects = {d.defect for d in check_mandate_health(inp)}
    assert MandateDefect.EXPIRY_BEFORE_DEBIT in defects


def test_afa_threshold_breach_above_15000_with_no_afa_scheduled():
    inp = healthy_input(
        mandate=MandateSnapshot(max_amount_paise=50_000_00, end_at=datetime(2027, 1, 1),
                                 status="active", afa_scheduled=False),
        upcoming_debit_paise=20_000_00,  # Rs 20,000
    )
    defects = {d.defect for d in check_mandate_health(inp)}
    assert MandateDefect.AFA_THRESHOLD_BREACH in defects


def test_afa_threshold_not_breached_when_afa_already_scheduled():
    inp = healthy_input(
        mandate=MandateSnapshot(max_amount_paise=50_000_00, end_at=datetime(2027, 1, 1),
                                 status="active", afa_scheduled=True),
        upcoming_debit_paise=20_000_00,
    )
    defects = {d.defect for d in check_mandate_health(inp)}
    assert MandateDefect.AFA_THRESHOLD_BREACH not in defects


def test_repeat_nsf_at_two_consecutive_returns():
    inp = healthy_input(mandate=MandateSnapshot(
        max_amount_paise=50_000_00, end_at=datetime(2027, 1, 1), status="active", consecutive_nsf=2,
    ))
    defects = {d.defect for d in check_mandate_health(inp)}
    assert MandateDefect.REPEAT_NSF in defects


def test_one_nsf_alone_does_not_trigger_repeat_nsf():
    inp = healthy_input(mandate=MandateSnapshot(
        max_amount_paise=50_000_00, end_at=datetime(2027, 1, 1), status="active", consecutive_nsf=1,
    ))
    defects = {d.defect for d in check_mandate_health(inp)}
    assert MandateDefect.REPEAT_NSF not in defects


def test_silent_revocation_when_revoked_and_no_cycle_attempted():
    inp = healthy_input(
        mandate=MandateSnapshot(max_amount_paise=50_000_00, end_at=datetime(2027, 1, 1), status="revoked"),
        cycle_was_attempted=False,
    )
    defects = {d.defect for d in check_mandate_health(inp)}
    assert MandateDefect.SILENT_REVOCATION in defects


def test_revoked_with_an_attempted_cycle_is_not_silent_revocation():
    """Revoked *and* we already tried (and presumably got an explicit decline) is
    a different, already-visible situation — not the "nobody noticed" case this
    detector exists for."""
    inp = healthy_input(
        mandate=MandateSnapshot(max_amount_paise=50_000_00, end_at=datetime(2027, 1, 1), status="revoked"),
        cycle_was_attempted=True,
    )
    defects = {d.defect for d in check_mandate_health(inp)}
    assert MandateDefect.SILENT_REVOCATION not in defects


def test_rail_degraded_above_threshold():
    inp = healthy_input(mandate=MandateSnapshot(
        max_amount_paise=50_000_00, end_at=datetime(2027, 1, 1), status="active",
        issuer_failure_rate=DEFAULT_ISSUER_FAILURE_RATE_THRESHOLD + 0.01,
    ))
    defects = {d.defect for d in check_mandate_health(inp)}
    assert MandateDefect.RAIL_DEGRADED in defects


def test_unknown_issuer_failure_rate_does_not_trigger_rail_degraded():
    """None means 'no data', not 'zero failures' — must not be treated as healthy
    by accident, but also must not spuriously trigger a defect with no evidence."""
    inp = healthy_input(mandate=MandateSnapshot(
        max_amount_paise=50_000_00, end_at=datetime(2027, 1, 1), status="active", issuer_failure_rate=None,
    ))
    defects = {d.defect for d in check_mandate_health(inp)}
    assert MandateDefect.RAIL_DEGRADED not in defects


def test_multiple_defects_can_be_detected_simultaneously():
    inp = healthy_input(
        mandate=MandateSnapshot(
            max_amount_paise=1_000_00, end_at=datetime(2026, 1, 1), status="active",
            afa_scheduled=False, consecutive_nsf=3,
        ),
        upcoming_debit_paise=20_000_00,
        next_debit_date=datetime(2026, 6, 1),
    )
    defects = {d.defect for d in check_mandate_health(inp)}
    assert MandateDefect.HEADROOM_BREACH in defects
    assert MandateDefect.EXPIRY_BEFORE_DEBIT in defects
    assert MandateDefect.AFA_THRESHOLD_BREACH in defects
    assert MandateDefect.REPEAT_NSF in defects


def test_every_detected_defect_carries_a_named_repair():
    inp = healthy_input(mandate=MandateSnapshot(
        max_amount_paise=50_000_00, end_at=datetime(2027, 1, 1), status="active", consecutive_nsf=5,
    ))
    for d in check_mandate_health(inp):
        assert d.repair
        assert d.detail
