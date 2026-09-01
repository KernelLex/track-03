"""Tests for eval/report.py's break-even classification -- the real bug
found while building this: a naive "first point where C >= A" search
reported the lowest grid point as "the break-even" even when C dominates
at every point (never depends on the swept parameter at all), which reads
as a real sensitivity threshold when there isn't one. BreakEven's three
kinds exist specifically so that can't happen silently again."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from eval.report import ASSUMED_MINUTES_PER_ESCALATION, BreakEven, _find_break_even, compute_economics
from eval.simulate import ArmSummary


@dataclass(frozen=True, slots=True)
class _FakeSummary:
    recovered_fraction: float


def _fake_lookup(a_fraction: float, c_fraction_fn):
    def lookup(lift: float, touch_cost_paise: int):
        return {"A": _FakeSummary(a_fraction), "C": _FakeSummary(c_fraction_fn(lift))}
    return lookup


class TestFindBreakEven:
    def test_c_dominating_at_every_point_is_not_reported_as_a_crossing(self):
        """The actual regression: C constant and always >= A must report
        'dominates_throughout', never a spurious lift value."""
        lookup = _fake_lookup(a_fraction=0.776, c_fraction_fn=lambda lift: 0.984)
        result = _find_break_even(touch_cost_paise=500, outcomes_by_lift_fn=lookup)
        assert result.kind == "dominates_throughout"
        assert result.value is None

    def test_a_genuine_crossing_is_found_and_reported_with_a_value(self):
        lookup = _fake_lookup(a_fraction=0.776, c_fraction_fn=lambda lift: min(0.1 + lift, 0.99))
        result = _find_break_even(touch_cost_paise=2_000_000, outcomes_by_lift_fn=lookup)
        assert result.kind == "crosses"
        assert result.value is not None
        assert 0.10 <= result.value <= 4.00
        # 0.1 + lift >= 0.776 => lift >= 0.676
        assert abs(result.value - 0.68) < 0.02

    def test_c_never_catching_up_is_reported_distinctly(self):
        lookup = _fake_lookup(a_fraction=0.99, c_fraction_fn=lambda lift: 0.01)
        result = _find_break_even(touch_cost_paise=2_000_000, outcomes_by_lift_fn=lookup)
        assert result.kind == "never_catches_up"
        assert result.value is None

    def test_break_even_kind_is_always_one_of_the_three_named_outcomes(self):
        for a, c_fn in [(0.5, lambda l: 0.9), (0.9, lambda l: 0.1), (0.5, lambda l: 0.4 + l * 0.1)]:
            result = _find_break_even(touch_cost_paise=1, outcomes_by_lift_fn=_fake_lookup(a, c_fn))
            assert result.kind in {"dominates_throughout", "crosses", "never_catches_up"}


def _summary(**overrides) -> ArmSummary:
    defaults = dict(
        arm="X", n=100, recovered_fraction=0.5, resolved_fraction=0.5, mean_days_to_resolution=5.0,
        human_escalation_rate=0.0, contact_exhausted_rate=0.0, mean_touches=2.0, total_bounds_violations=0,
    )
    defaults.update(overrides)
    return ArmSummary(**defaults)


class TestComputeEconomics:
    def test_autonomy_rate_is_the_inverse_of_human_escalation_rate(self):
        economics = compute_economics({"A": _summary(human_escalation_rate=0.3)}, touch_cost_paise=500)
        assert economics["A"]["autonomy_rate"] == pytest.approx(0.7)

    def test_zero_escalations_yields_zero_human_minutes_per_recovery(self):
        economics = compute_economics({"A": _summary(human_escalation_rate=0.0, resolved_fraction=0.8)}, touch_cost_paise=500)
        assert economics["A"]["human_minutes_per_recovery"] == 0.0

    def test_human_minutes_per_recovery_scales_with_the_declared_assumption(self):
        economics = compute_economics(
            {"A": _summary(human_escalation_rate=0.5, resolved_fraction=0.5, n=100)}, touch_cost_paise=500,
        )
        # 50 escalations * ASSUMED_MINUTES_PER_ESCALATION minutes / 50 resolved
        assert economics["A"]["human_minutes_per_recovery"] == pytest.approx(ASSUMED_MINUTES_PER_ESCALATION)

    def test_zero_recovery_yields_none_not_a_zero_division_crash(self):
        economics = compute_economics({"A": _summary(recovered_fraction=0.0)}, touch_cost_paise=500)
        assert economics["A"]["cost_per_rupee_recovered"] is None

    def test_zero_resolved_yields_none_human_minutes_not_a_crash(self):
        economics = compute_economics({"A": _summary(resolved_fraction=0.0, human_escalation_rate=0.0)}, touch_cost_paise=500)
        assert economics["A"]["human_minutes_per_recovery"] is None

    def test_cost_per_touch_paise_is_reported_as_given(self):
        economics = compute_economics({"A": _summary()}, touch_cost_paise=12345)
        assert economics["A"]["cost_per_touch_paise"] == 12345

    def test_higher_touch_cost_raises_cost_per_rupee_recovered(self):
        cheap = compute_economics({"A": _summary()}, touch_cost_paise=500)
        expensive = compute_economics({"A": _summary()}, touch_cost_paise=50_000)
        assert expensive["A"]["cost_per_rupee_recovered"] > cheap["A"]["cost_per_rupee_recovered"]
