"""Arm A's fixed dunning schedule (DEVDOC_v6 §17.4) — the one arm testable
without an LLM or a persona-simulation engine."""

from __future__ import annotations

from eval.arms.a.schedule import FIXED_SCHEDULE_DAYS, all_touches_up_to, touch_for_day


def test_no_touch_on_a_day_outside_the_schedule():
    assert touch_for_day(0) is None
    assert touch_for_day(3) is None
    assert touch_for_day(100) is None


def test_a_touch_on_every_scheduled_day():
    for day in FIXED_SCHEDULE_DAYS:
        assert touch_for_day(day) is not None


def test_the_last_scheduled_day_is_marked_final_and_uses_a_statutory_notice():
    last_day = FIXED_SCHEDULE_DAYS[-1]
    touch = touch_for_day(last_day)
    assert touch.is_final is True
    assert touch.action_type == "send_statutory_notice"


def test_earlier_scheduled_days_are_reminders_not_final():
    for day in FIXED_SCHEDULE_DAYS[:-1]:
        touch = touch_for_day(day)
        assert touch.is_final is False
        assert touch.action_type == "send_reminder"


def test_all_touches_up_to_accumulates_only_scheduled_days_so_far():
    touches = all_touches_up_to(10)
    assert [t.day for t in touches] == [d for d in FIXED_SCHEDULE_DAYS if d <= 10]


def test_all_touches_up_to_zero_days_is_empty():
    assert all_touches_up_to(0) == []


def test_all_touches_up_to_far_future_includes_the_whole_schedule():
    touches = all_touches_up_to(365)
    assert [t.day for t in touches] == list(FIXED_SCHEDULE_DAYS)


def test_schedule_is_deterministic_and_reactionless():
    """Arm A's entire point as a control: the same day always produces the
    same touch, regardless of any diagnosis or debtor state -- the function
    signature itself enforces this (it takes only an int)."""
    assert touch_for_day(7) == touch_for_day(7)
