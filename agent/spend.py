"""Real, persistent tracking of every real Anthropic API call this project
makes, plus a hard budget ceiling. Not a DEVDOC_v6 requirement — a
project-level constraint the user set explicitly (a $20 total ceiling on
API spend), enforced the way this codebase enforces everything else that
matters: a real gate checked before the fact, backed by an append-only
record, not a promise held in a conversation.

Deliberately simpler than agent.ledger.store.Ledger: no hash chain, no
tamper-evidence. This tracks a budget, not money owed by a debtor — an
honest running total is the whole job, not resistance to a malicious actor
editing the file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BUDGET_CEILING_USD = 20.0
"""The user's explicit instruction: do not spend more than this, total,
across every real Anthropic call this project makes."""

DEFAULT_LEDGER_PATH = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "api_spend.jsonl"
"""Committed, not gitignored — docs/evidence/ is deliberately tracked in
git (see .gitignore's own comment) so this spend record is a real, honest,
inspectable artifact, not a local file nobody but this session ever sees."""

MODEL_PRICING_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-opus-5": {"input": 5.0, "output": 25.0},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}
"""Current published per-1M-token pricing for the models this project might
call. Deliberately a closed table, not a guess-and-continue fallback — see
UnknownModelPricing."""

CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1
"""A cache write costs ~1.25x the base input price; a cache read ~0.1x —
both priced off the same model's input rate, not a separate cache rate."""


class UnknownModelPricing(Exception):
    """Raised rather than silently treating an unpriced model as free —
    the one failure mode a budget tracker must never have."""


class BudgetExceeded(Exception):
    """Raised BEFORE a call whose estimated cost would push cumulative
    spend over BUDGET_CEILING_USD. Never raised after the fact — by the
    time a call has actually happened, refusing it is too late to matter."""


def _pricing_for(model: str) -> dict[str, float]:
    if model not in MODEL_PRICING_USD_PER_MTOK:
        raise UnknownModelPricing(
            f"no pricing known for model {model!r} -- add it to "
            "agent.spend.MODEL_PRICING_USD_PER_MTOK before calling it"
        )
    return MODEL_PRICING_USD_PER_MTOK[model]


def estimate_cost_usd(*, model: str, input_tokens: int, output_tokens: int) -> float:
    """A worst-case pre-call estimate (no cache assumed) — used to decide
    whether to even attempt a call, not recorded as what it actually cost."""
    pricing = _pricing_for(model)
    return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000


def actual_cost_usd(
    *, model: str, input_tokens: int, output_tokens: int,
    cache_creation_input_tokens: int = 0, cache_read_input_tokens: int = 0,
) -> float:
    pricing = _pricing_for(model)
    raw = (
        input_tokens * pricing["input"]
        + output_tokens * pricing["output"]
        + cache_creation_input_tokens * pricing["input"] * CACHE_WRITE_MULTIPLIER
        + cache_read_input_tokens * pricing["input"] * CACHE_READ_MULTIPLIER
    )
    return raw / 1_000_000


@dataclass(frozen=True, slots=True)
class SpendRecord:
    ts: str
    model: str
    purpose: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    cost_usd: float
    is_estimated: bool = False
    """True only for the fallback path where a call's real output-token
    count was unrecoverable (agent.diagnose.llm_extract: the SDK's
    `.parse()` raises a ValidationError *after* the billed call already
    happened but before returning the response, so `response.usage` is
    never reachable — found live, not theoretical, see docs/LLM_EXTRACTION.md).
    input_tokens is still the real pre-call count from count_tokens() in
    that case; only output_tokens is a worst-case (max_tokens) upper bound,
    chosen because overestimating spend is the safe direction for a budget
    ceiling — never False by omission; every accurately-recorded call sets
    this explicitly False, not by relying on the default."""

    def to_dict(self) -> dict:
        return {
            "ts": self.ts, "model": self.model, "purpose": self.purpose,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "is_estimated": self.is_estimated,
        }


class SpendLedger:
    def __init__(self, path: Path | str = DEFAULT_LEDGER_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def total_spent_usd(self) -> float:
        if not self.path.exists():
            return 0.0
        total = 0.0
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    total += json.loads(line)["cost_usd"]
        return total

    def remaining_budget_usd(self) -> float:
        return BUDGET_CEILING_USD - self.total_spent_usd()

    def check_budget(self, estimated_additional_cost_usd: float) -> None:
        spent = self.total_spent_usd()
        if spent + estimated_additional_cost_usd > BUDGET_CEILING_USD:
            raise BudgetExceeded(
                f"this call's estimated cost (${estimated_additional_cost_usd:.4f}) would push "
                f"cumulative spend (${spent:.4f} already recorded) over the "
                f"${BUDGET_CEILING_USD:.2f} ceiling -- refusing before making the call"
            )

    def record(
        self, *, model: str, purpose: str, input_tokens: int, output_tokens: int,
        cache_creation_input_tokens: int = 0, cache_read_input_tokens: int = 0,
        is_estimated: bool = False,
    ) -> SpendRecord:
        cost = actual_cost_usd(
            model=model, input_tokens=input_tokens, output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
        )
        record = SpendRecord(
            ts=datetime.now(timezone.utc).isoformat(), model=model, purpose=purpose,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens, cost_usd=cost,
            is_estimated=is_estimated,
        )
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record.to_dict()) + "\n")
        return record
