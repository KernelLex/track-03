# The Monte Carlo simulation harness

`eval/personas/generator.py` + `eval/simulate.py`. Compares Arms A / B2 / C
over a synthetic population, with zero real model calls — this measures
what the deterministic pipeline (`compute_ev()`, `check_bounds()`) does in
aggregate, which needs no LLM, the same principle
`tests/agent/test_injection_resistance.py` already uses elsewhere in this
codebase. See `eval/PREREGISTRATION.md` for how this fits DEVDOC_v6 §17's
pre-registration discipline, and each module's own docstring for the full
FITTED-vs-ASSUMED breakdown of every constant.

## Running it

```
uv run trucommit simulate --n 300 --seed 1 --lift 2.0
uv run python eval/simulate.py --n 300 --seed 1 --lift 2.0   # equivalent
```

```
Arm      n  Recovered  Resolved  Avg days  Human esc   Lost  Touches
A      300     79.0%     78.7%       5.0      0.0%   9.7%     2.08
B2     300     94.6%     95.7%       6.9      0.0%   4.0%     1.86
C      300     97.4%     97.0%       6.5     10.3%   0.3%     1.76
```

(One real run, seed 1 — numbers move a little with a different seed, but
the ordering is stable; see `tests/eval/test_simulate.py`'s structural
invariant tests for what's actually guaranteed to hold, not just observed
once.)

## What this is and isn't

**Is**: a genuine exercise of real project code. Arm C's per-touch loop
calls `agent.decide.ev.compute_ev()` and `agent.bounds.engine.check_bounds()`
directly — not a reimplementation standing in for them. The population's
`amount_paise`, dispute rate, and `p_base` all trace to the fitted Kaggle
parameters in `data/fitted_params.yaml`. The comparison is reproducible
(same seed → byte-identical result, tested) and fair (all three arms see
the identical population).

**Isn't**: a pre-registered result, or evidence that TrueCommit "beats" a
fixed schedule by the specific margins printed above. Three things make
that true, on purpose:

1. **Diagnose is simulated, not real.** There's no free text to extract
   from a synthetic persona, so a `DIAGNOSTIC_ACCURACY` draw (0.85, a
   declared prior) stands in for a real extractor's accuracy. A real
   extractor's actual accuracy is unmeasured — no golden set exists yet
   (§17.8).
2. **The outcome-probability constants are declared priors, not fitted
   values** (`ASSUMED_RESOLUTION_PROB_MATCHED`/`_MISMATCHED` in
   `eval/simulate.py`). DEVDOC_v6 §17.1 is explicit that no dataset gives
   real intervention-response data for Indian B2B AR — these numbers are
   chosen to be plausible and internally consistent (matched ≥ mismatched
   for every real blocker type), not fitted or tuned to produce a
   particular headline gap.
3. **Population size, window length, and the primary comparison metric are
   still marked `PENDING` in `eval/PREREGISTRATION.md`**, deliberately.
   Building and testing this harness (including the sample run above) is
   not the same act as running a pre-registered evaluation — DEVDOC_v6
   §17.6 exists specifically so that choice happens *before* anyone has
   seen a result, in its own commit.

## A real finding from building this: `--lift` and `EV_FLOOR`

At the default touch cost (Rs 5) against a population with a ~Rs 50,000
median invoice, `EV_FLOOR` essentially never refuses anywhere in the
declared `lift_prior` sweep range (0.5x–4.0x, §17.2) — the recoverable
amount dwarfs a five-rupee touch cost regardless of lift. `--lift` alone
will look like it does nothing at default settings. That's a real property
of this population (money at stake is large relative to the cost of
sending a message), not a bug — confirmed by raising `--touch-cost-paise`
into the tens of thousands, where `--lift` starts changing Arm C's outcome
dramatically (down to under 1% recovered at low lift and high cost, since
`EV_FLOOR` correctly refuses almost every touch):

```
uv run trucommit simulate --n 500 --lift 0.5 --touch-cost-paise 4500000   # ~0% recovered -- EV_FLOOR refuses almost everything
uv run trucommit simulate --n 500 --lift 4.0 --touch-cost-paise 4500000   # ~98% recovered
```

`tests/eval/test_simulate.py::test_ev_floor_actually_refuses_when_cost_dominates_recoverable_amount`
is a regression test for exactly this, so a future change can't silently
make the gate stop mattering without a test noticing.

## What's next

Locking in `eval/PREREGISTRATION.md`'s remaining `PENDING` rows and running
the first arm comparison that counts as evidence — see `PROGRESS.md`.
