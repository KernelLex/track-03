# API spend tracking

The user's explicit instruction: don't spend more than $20 total on real
Anthropic API calls, and keep track of what's been spent. `agent/spend.py`
enforces this as a real gate, not a remembered promise — the same
philosophy this codebase applies to money owed by debtors, applied here to
money owed to Anthropic.

## How it works

- **Before** every real call (`agent/diagnose/llm_extract.py::extract_from_reply`),
  a real token count for that exact request is fetched via
  `client.messages.count_tokens(...)`, a worst-case cost is estimated
  (assuming the full `max_tokens` gets used and no caching applies), and
  `SpendLedger.check_budget()` raises `BudgetExceeded` *before* the
  generating call if that estimate would push cumulative spend over
  $20 — refused, not billed.
- **After** a successful call, the real usage from `response.usage`
  (including cache creation/read tokens, each priced at their own rate —
  a cache write costs ~1.25x a plain input token, a cache read ~0.1x) is
  recorded to `docs/evidence/api_spend.jsonl`, an append-only, git-committed
  log — not a local file only this session can see.

## A real gap found and fixed while building this

The first live call (2026-08-31) surfaced a genuine bug: when the model's
output is JSON-schema-valid but fails one of `ExtractionResult`'s own
Pydantic validators, the Anthropic SDK's `messages.parse()` raises
`pydantic.ValidationError` from *inside* its own response-parsing step —
after the billed call already happened, but without ever returning the
response object, so `response.usage` is unreachable. Silently skipping the
record in that path would have under-counted real spend against the
ceiling — the opposite of what "keep track" means. Fixed by recording a
conservative estimate in that specific path (`SpendRecord.is_estimated=True`):
the real input-token count is still available from the pre-call count, only
`output_tokens` falls back to a worst-case `max_tokens` bound. Regression
test: `tests/agent/test_llm_extract.py::
test_a_validation_error_from_parse_itself_still_records_a_conservative_estimate`.

That same first call also exposed *why* validation failed: the model
returned `promise.date="October 1st"` verbatim (correctly reading the
debtor's words, but with no way to resolve which year "October 1st" means)
instead of ISO8601. Fixed by adding a second, small, *uncached* system
block carrying today's date, placed after the large cacheable instruction
block so it doesn't invalidate the cache prefix.

## Live evidence, 2026-08-31

Two real calls, both against `claude-sonnet-5`, via `tools/verify_credentials.py`:

```
{"input_tokens": 92,  "output_tokens": 234, "cache_creation_input_tokens": 3378, "cache_read_input_tokens": 0,    "cost_usd": 0.010969}
{"input_tokens": 95,  "output_tokens": 121, "cache_creation_input_tokens": 0,    "cache_read_input_tokens": 3378, "cost_usd": 0.002076}
```

Total: **$0.013050** of the $20 ceiling. The second call's
`cache_read_input_tokens` exactly matches the first call's
`cache_creation_input_tokens` (3,378) — real, live confirmation that
prompt caching is working as designed, not just plausible in theory: the
second call cost roughly a fifth of the first for a comparable amount of
work. Both extractions were also correct on inspection: "we will pay the
full amount by October 1st" → `family=C, class=PROMISE_STATED,
confidence=0.88`; "bills 200 units but we only received 150" →
`family=D, class=QUANTITY_QUALITY, confidence=0.94`.

## Checking spend

```
uv run python tools/verify_credentials.py    # prints spend before/after, makes 1-2 tiny real calls
```

```python
from agent.spend import SpendLedger
SpendLedger().total_spent_usd()          # cumulative, all-time
SpendLedger().remaining_budget_usd()     # $20 minus that
```

`docs/evidence/api_spend.jsonl` is the source of truth — every real call
any part of this project makes should route through `SpendLedger`
(pass `spend_ledger=` explicitly, or rely on the default, which always
points at this same committed file) so the total stays accurate across
every script, test-with-a-real-key, and future simulation run that spends
real money.
