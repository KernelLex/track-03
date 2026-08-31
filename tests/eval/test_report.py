"""Tests for eval/report.py's break-even classification -- the real bug
found while building this: a naive "first point where C >= A" search
reported the lowest grid point as "the break-even" even when C dominates
at every point (never depends on the swept parameter at all), which reads
as a real sensitivity threshold when there isn't one. BreakEven's three
kinds exist specifically so that can't happen silently again."""

from __future__ import annotations

from dataclasses import dataclass

from eval.report import BreakEven, _find_break_even


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
