"""agent.spend -- the pricing math and the budget gate in isolation."""

from __future__ import annotations

import json

import pytest

from agent.spend import (
    BUDGET_CEILING_USD,
    BudgetExceeded,
    SpendLedger,
    UnknownModelPricing,
    actual_cost_usd,
    estimate_cost_usd,
)


class TestCostMath:
    def test_estimate_matches_known_sonnet_pricing(self):
        # 1,000,000 input tokens @ $2, 1,000,000 output tokens @ $10
        cost = estimate_cost_usd(model="claude-sonnet-5", input_tokens=1_000_000, output_tokens=1_000_000)
        assert cost == pytest.approx(12.0)

    def test_unknown_model_raises_rather_than_pricing_as_free(self):
        with pytest.raises(UnknownModelPricing):
            estimate_cost_usd(model="some-future-model", input_tokens=100, output_tokens=100)
        with pytest.raises(UnknownModelPricing):
            actual_cost_usd(model="some-future-model", input_tokens=100, output_tokens=100)

    def test_cache_write_costs_more_than_a_plain_input_token(self):
        plain = actual_cost_usd(model="claude-sonnet-5", input_tokens=1000, output_tokens=0)
        cached_write = actual_cost_usd(
            model="claude-sonnet-5", input_tokens=0, output_tokens=0, cache_creation_input_tokens=1000,
        )
        assert cached_write > plain

    def test_cache_read_costs_less_than_a_plain_input_token(self):
        plain = actual_cost_usd(model="claude-sonnet-5", input_tokens=1000, output_tokens=0)
        cached_read = actual_cost_usd(
            model="claude-sonnet-5", input_tokens=0, output_tokens=0, cache_read_input_tokens=1000,
        )
        assert 0 < cached_read < plain


class TestSpendLedger:
    def test_starts_at_zero_with_no_file(self, tmp_path):
        ledger = SpendLedger(tmp_path / "spend.jsonl")
        assert ledger.total_spent_usd() == 0.0
        assert ledger.remaining_budget_usd() == BUDGET_CEILING_USD

    def test_record_appends_and_total_accumulates(self, tmp_path):
        ledger = SpendLedger(tmp_path / "spend.jsonl")
        ledger.record(model="claude-sonnet-5", purpose="a", input_tokens=1000, output_tokens=1000)
        ledger.record(model="claude-sonnet-5", purpose="b", input_tokens=1000, output_tokens=1000)
        first_call_cost = actual_cost_usd(model="claude-sonnet-5", input_tokens=1000, output_tokens=1000)
        assert ledger.total_spent_usd() == pytest.approx(first_call_cost * 2)

    def test_survives_reopening_the_same_path(self, tmp_path):
        path = tmp_path / "spend.jsonl"
        SpendLedger(path).record(model="claude-sonnet-5", purpose="a", input_tokens=1000, output_tokens=1000)
        reopened = SpendLedger(path)
        assert reopened.total_spent_usd() > 0

    def test_check_budget_passes_when_well_under_ceiling(self, tmp_path):
        ledger = SpendLedger(tmp_path / "spend.jsonl")
        ledger.check_budget(0.01)  # should not raise

    def test_check_budget_raises_before_exceeding_the_ceiling(self, tmp_path):
        ledger = SpendLedger(tmp_path / "spend.jsonl")
        with pytest.raises(BudgetExceeded):
            ledger.check_budget(BUDGET_CEILING_USD + 0.01)

    def test_check_budget_accounts_for_prior_spend_not_just_this_call(self, tmp_path):
        ledger = SpendLedger(tmp_path / "spend.jsonl")
        ledger.record(model="claude-sonnet-5", purpose="prior", input_tokens=0, output_tokens=1_999_000)
        spent_so_far = ledger.total_spent_usd()
        assert spent_so_far < BUDGET_CEILING_USD  # sanity: setup didn't already blow the budget
        with pytest.raises(BudgetExceeded):
            ledger.check_budget(BUDGET_CEILING_USD - spent_so_far + 0.01)

    def test_record_is_valid_json_lines(self, tmp_path):
        path = tmp_path / "spend.jsonl"
        ledger = SpendLedger(path)
        ledger.record(model="claude-sonnet-5", purpose="a", input_tokens=10, output_tokens=10)
        ledger.record(model="claude-sonnet-5", purpose="b", input_tokens=20, output_tokens=20)
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            row = json.loads(line)  # must not raise
            assert "cost_usd" in row and "ts" in row

    def test_cost_is_never_recorded_as_a_negative_number(self, tmp_path):
        ledger = SpendLedger(tmp_path / "spend.jsonl")
        record = ledger.record(model="claude-sonnet-5", purpose="a", input_tokens=10, output_tokens=10)
        assert record.cost_usd >= 0
