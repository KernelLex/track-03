"""Path A structured diagnosis: every code in DEVDOC_v6 §11.2's worked example resolves,
and the RETRYABLE/TERMINAL split matches the doc's own stated logic exactly."""

from __future__ import annotations

import pytest

from agent.diagnose.taxonomy import UnknownFailureCode, default_taxonomy


@pytest.fixture(scope="module")
def taxonomy():
    return default_taxonomy()


def test_loads_a_nontrivial_number_of_codes(taxonomy):
    assert len(taxonomy) >= 20


@pytest.mark.parametrize(
    "code,rail,expected_disposition",
    [
        ("insufficient_funds", "cards", "RETRYABLE"),
        ("card_expired", "cards", "TERMINAL"),
        ("payment_cancelled", "cards", "TERMINAL"),
        ("payment_cancelled", "upi", "TERMINAL"),
        ("insufficient_funds", "upi", "RETRYABLE"),
    ],
)
def test_matches_devdoc_worked_examples(taxonomy, code, rail, expected_disposition):
    result = taxonomy.classify(code, rail)
    assert result.disposition == expected_disposition


def test_unknown_code_raises_rather_than_silently_defaulting(taxonomy):
    with pytest.raises(UnknownFailureCode):
        taxonomy.classify("not_a_real_code", "cards")


def test_every_entry_has_a_non_empty_description(taxonomy):
    for code in taxonomy.permitted_codes("cards"):
        result = taxonomy.classify(code, "cards")
        assert result.description.strip()


def test_permitted_codes_split_by_rail_is_disjoint_from_empty(taxonomy):
    cards_codes = taxonomy.permitted_codes("cards")
    upi_codes = taxonomy.permitted_codes("upi")
    assert len(cards_codes) > 0
    assert len(upi_codes) > 0
    assert taxonomy.permitted_codes() == cards_codes | upi_codes
