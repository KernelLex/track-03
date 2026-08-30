"""Path B's extraction schema: family/class consistency, and schema-poisoning
rejection at the Pydantic boundary (§11.2, §24.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.diagnose.extract import ACTIONS_UNLOCKED, DiagnosisClass, ExtractionResult, Family


def test_valid_extraction_round_trips():
    result = ExtractionResult.model_validate({
        "family": "C", "class": "PROMISE_STATED", "confidence": 0.8,
        "promise": {"amount_paise": 50000, "date": "2026-09-01", "installments": None},
        "objection_signal": False,
    })
    assert result.family == Family.C
    assert result.class_ == DiagnosisClass.PROMISE_STATED


def test_class_must_belong_to_its_family():
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate({
            "family": "A", "class": "PROMISE_STATED", "confidence": 0.9,  # PROMISE_STATED is family C
        })


def test_confidence_out_of_range_is_rejected():
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate({"family": "A", "class": "INSUFFICIENT_FUNDS", "confidence": 1.5})


def test_extra_fields_are_rejected_not_silently_dropped():
    """Schema poisoning (§24.1): a crafted extra field must fail validation
    outright, not pass through as an ignored key."""
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate({
            "family": "C", "class": "PROMISE_STATED", "confidence": 0.9,
            "state": "RECOVERED",  # not a real field -- an attempted injection
        })


def test_every_family_has_a_nonempty_action_set_and_none_include_settlement_actions():
    dangerous_actions = {"no_action"}  # no_action is fine; nothing family-unlocked should mark money settled
    for family, actions in ACTIONS_UNLOCKED.items():
        assert len(actions) > 0
        assert "mark_settled" not in actions
        assert "close_account" not in actions


def test_family_d_unlocks_only_escalate_human():
    assert ACTIONS_UNLOCKED[Family.D] == frozenset({"escalate_human"})


def test_promise_amount_of_exactly_zero_is_rejected():
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate({
            "family": "C", "class": "PROMISE_STATED", "confidence": 0.9,
            "promise": {"amount_paise": 0},
        })


def test_promise_date_decades_out_is_rejected():
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate({
            "family": "C", "class": "PROMISE_STATED", "confidence": 0.9,
            "promise": {"date": "2099-12-31"},
        })


def test_promise_date_far_in_the_past_is_rejected():
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate({
            "family": "C", "class": "PROMISE_STATED", "confidence": 0.9,
            "promise": {"date": "1999-01-01"},
        })


def test_promise_date_malformed_string_is_rejected():
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate({
            "family": "C", "class": "PROMISE_STATED", "confidence": 0.9,
            "promise": {"date": "not-a-date"},
        })


def test_promise_date_within_a_plausible_near_term_horizon_is_accepted():
    from datetime import date, timedelta
    near_future = (date.today() + timedelta(days=30)).isoformat()
    result = ExtractionResult.model_validate({
        "family": "C", "class": "PROMISE_STATED", "confidence": 0.9,
        "promise": {"date": near_future},
    })
    assert result.promise.date == near_future


def test_malformed_gstin_is_rejected():
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate({
            "family": "B", "class": "GST_DEFECT", "confidence": 0.9,
            "entities": {"gstin": "DROP TABLE invoices;"},
        })


def test_well_formed_gstin_is_accepted():
    result = ExtractionResult.model_validate({
        "family": "B", "class": "GST_DEFECT", "confidence": 0.9,
        "entities": {"gstin": "29ABCDE1234F1Z5"},
    })
    assert result.entities.gstin == "29ABCDE1234F1Z5"


def test_amount_paise_far_out_of_any_sane_range_still_validates_as_a_type_but_is_just_an_int():
    """The schema doesn't itself bound amount_paise -- that's select_instrument's
    and check_bounds' job downstream. This test documents that boundary
    explicitly rather than leaving it assumed."""
    result = ExtractionResult.model_validate({
        "family": "C", "class": "PROMISE_STATED", "confidence": 0.9,
        "promise": {"amount_paise": 999_999_999_999},
    })
    assert result.promise.amount_paise == 999_999_999_999
