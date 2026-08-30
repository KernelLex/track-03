"""§8's deemed-acceptance worked example, exercised directly. The asymmetry
under test: a false positive on objection_marker costs a human review; a
false negative must be structurally impossible to produce a wrong legal claim."""

from __future__ import annotations

from datetime import datetime

from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family
from agent.diagnose.objection import (
    CommsLogEntry,
    compute_deemed_acceptance,
    compute_objection_marker,
    text_matches_objection_lexicon,
)

CONFIDENT_PROMISE = ExtractionResult.model_validate({
    "family": "C", "class": "PROMISE_STATED", "confidence": 0.95,
})


def test_confident_non_dispute_extraction_does_not_set_the_marker():
    marker = compute_objection_marker(
        CONFIDENT_PROMISE, raw_text="I'll pay by Friday", confidence_threshold=0.5, extractor_quarantined=False,
    )
    assert marker is False


def test_family_d_always_sets_the_marker_regardless_of_confidence():
    dispute = ExtractionResult.model_validate({"family": "D", "class": "AMOUNT", "confidence": 0.99})
    marker = compute_objection_marker(
        dispute, raw_text="looks fine to me", confidence_threshold=0.5, extractor_quarantined=False,
    )
    assert marker is True


def test_low_confidence_sets_the_marker_even_with_a_clean_family():
    marker = compute_objection_marker(
        CONFIDENT_PROMISE, raw_text="ok", confidence_threshold=0.99, extractor_quarantined=False,
    )
    assert marker is True


def test_lexicon_hit_sets_the_marker_even_with_high_confidence_and_clean_family():
    marker = compute_objection_marker(
        CONFIDENT_PROMISE, raw_text="the goods arrived damaged", confidence_threshold=0.5, extractor_quarantined=False,
    )
    assert marker is True


def test_quarantined_extractor_sets_the_marker_for_everything():
    marker = compute_objection_marker(
        CONFIDENT_PROMISE, raw_text="all good, will pay", confidence_threshold=0.1, extractor_quarantined=True,
    )
    assert marker is True


def test_hinglish_lexicon_terms_are_matched():
    assert text_matches_objection_lexicon("maal kam hai bhai")
    assert text_matches_objection_lexicon("yeh order galat bheja hai")


# ---- Deemed acceptance ----


def test_deemed_accepted_when_no_objection_in_window():
    result = compute_deemed_acceptance(
        acceptance_date=datetime(2026, 1, 1), objection_window_days=15,
        comms=[CommsLogEntry(direction="outbound", received_at=datetime(2026, 1, 5), objection_marker=False)],
    )
    assert result.deemed_accepted is True
    assert result.escalate_to_human_queue is False


def test_not_deemed_accepted_when_an_inbound_objection_marker_falls_in_window():
    result = compute_deemed_acceptance(
        acceptance_date=datetime(2026, 1, 1), objection_window_days=15,
        comms=[CommsLogEntry(direction="inbound", received_at=datetime(2026, 1, 10), objection_marker=True)],
    )
    assert result.deemed_accepted is False
    assert result.escalate_to_human_queue is True


def test_objection_marker_outside_the_window_does_not_block_acceptance():
    result = compute_deemed_acceptance(
        acceptance_date=datetime(2026, 1, 1), objection_window_days=15,
        comms=[CommsLogEntry(direction="inbound", received_at=datetime(2026, 3, 1), objection_marker=True)],
    )
    assert result.deemed_accepted is True


def test_outbound_message_with_objection_marker_does_not_count():
    """objection_marker on an outbound message (something *we* sent) is not a
    debtor objection -- only inbound counts, per §8's own EXISTS query."""
    result = compute_deemed_acceptance(
        acceptance_date=datetime(2026, 1, 1), objection_window_days=15,
        comms=[CommsLogEntry(direction="outbound", received_at=datetime(2026, 1, 10), objection_marker=True)],
    )
    assert result.deemed_accepted is True


def test_no_comms_at_all_defaults_to_accepted():
    result = compute_deemed_acceptance(acceptance_date=datetime(2026, 1, 1), objection_window_days=15, comms=[])
    assert result.deemed_accepted is True
