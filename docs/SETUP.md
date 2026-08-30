# Setup

## The ten-minute promise (DEVDOC_v6 §19)

```
git clone <this repo>
cd track-03
uv sync
uv run trucommit demo
uv run pytest
```

**This has been verified end to end** by cloning the committed repository
into a scratch directory and timing it (2026-08-30): `uv sync` completed in
under 2 seconds with a warm local package cache (expect longer — still well
under ten minutes — on a fully cold cache, since it's ~35 small pure-Python
packages), `uv run trucommit demo` ran and printed real output in under a
second, and `uv run pytest` passed all 334 tests in about 27 seconds.

If `uv` isn't already on your machine:

```
pip install uv
uv python install 3.12      # pins the exact interpreter this project targets
```

`uv sync` will then create `.venv/` and install everything from
`pyproject.toml` / `uv.lock`, including the project itself in editable mode
(`[tool.uv] package = true`), which is what makes `import agent...` and the
`trucommit` console script both work without any extra `pip install -e .`
step.

## What `trucommit demo` actually does

It is **not** the four-arm evaluation from DEVDOC_v6 §17 — that needs
persona definitions and a committed pre-registration that don't exist yet
(see `docs/LIMITATIONS.md`). It is a small, real, honestly-scoped walk of
one synthetic debtor through the pieces that are built: the debtor state
machine, `select_instrument()`, `check_bounds()`, `SimulatedRail`, and the
`recovery_ledger`'s attribution, ending with a verified hash-chained ledger.
Every number it prints comes from actually running that code, not from a
hand-typed transcript.

## Running the test suite

```
uv run pytest                    # all 334 tests
uv run pytest tests/agent/test_bounds_differential.py   # the 5,000-example differential test alone (~13s)
uv run pytest -k "not differential"                     # skip the slowest test if iterating quickly
```

## Regenerating documentation

```
uv run python tools/gen_docs.py            # regenerates docs/BOUNDS.md, REGULATORY_MAP.md, LEDGER.md
uv run python tools/gen_docs.py --check    # exit 1 if the committed docs are stale (CI gate)
```

## Running the day-zero rail probe

Requires a free Razorpay test-mode account (no KYC needed — DEVDOC_v6 §5.1):

```
RAZORPAY_KEY_ID=rzp_test_xxx RAZORPAY_KEY_SECRET=xxx uv run python tools/probe_rails.py
```

This has **not** been run yet in this build (no test keys were available)
— see `docs/RAIL_CAPABILITIES.md`. Running it is the single highest-value
next step: everything else in the mandate/instrument layer already works
against `SimulatedRail` regardless of the outcome (§5.2 — "the judgment is
the product, the rail is plumbing"), but the live-verified claims in a
README or pitch video depend on this having actually run.

## Environment

- Python 3.12 (pinned via `uv python install 3.12`; the project also runs
  under whatever Python `uv` resolves, but 3.12 is what DEVDOC_v6 §19 targets)
- SQLite (bundled with Python — no server to stand up)
- No Postgres, Redis, Celery, or Node.js required for anything built so far
