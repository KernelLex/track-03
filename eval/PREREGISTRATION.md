# Pre-Registration

DEVDOC_v6 §17.6: committed *before* any arm runs. "Costs nothing, takes an
hour, and is the general-form answer to every 'you chose that number'
objection." `RESULTS.md` will cite this file's git commit hash once it
exists.

**Status: most Fitted parameters are now actually fitted; harness not yet
built.** No arm has run. This file exists so that when the harness is
built, fitting or sweeping a parameter is a matter of filling in a value
already committed to a class — never a choice made after seeing a result.

**Correction, 2026-08-31**: this file previously marked the two Kaggle
datasets `PENDING`, on the assumption that Kaggle access needs an account
and API token. That's true of the classic `kaggle` CLI/API, but
`kagglehub.dataset_download(...)` fetches public datasets anonymously —
no auth at all. Both datasets are now committed in `data/ar_seed/` and
fitted by `tools/fit_persona_params.py`, output in `data/fitted_params.yaml`.
Parameters still genuinely blocked (Atradius, published dunning benchmarks)
remain marked `PENDING` below, honestly — that block wasn't a wrong
assumption, just not yet done.

## Parameter classification (§17.2)

| Parameter | Class | Value | Basis |
|---|---|---|---|
| Invoice amount and term distributions | Fitted | **Done** (2026-08-31) — mean $59.90, median $60.56, std $20.44 (USD, shape only — see `data/fitted_params.yaml`) | IBM AR set, `tools/fit_persona_params.py` |
| `DaysLate` distribution conditional on `Disputed` | Fitted | **Done** — disputed: mean 8.58d / median 7d; not disputed: mean 1.93d / median 0d | IBM AR set |
| Dispute base rate | Fitted | **Done** — 0.2275 (22.75% of 2,466 invoices) | IBM AR set |
| `p_base` payment-date model | Fitted | **Done** — logistic regression on log1p(amount), holdout Brier score 0.0206 (n=8,000). Honest caveat: the holdout base rate is 97.9% (almost everything pays within 30 days regardless of amount in this dataset), so this Brier score reflects a well-calibrated but weakly discriminative model, not a strong predictive signal — see `agent/decide/fitted_p_base.py` | Payment Date Prediction dataset, 50,000 rows |
| Overdue share, DSO | Fitted | **PENDING** — needs the Atradius Payment Practices Barometer (Asia), not yet pulled into this build | Atradius |
| Card-retry recovery base rate | Fitted | **PENDING** — needs published dunning benchmarks, not yet sourced | Published benchmarks |
| Instrument-conversion lift vs message | Swept | **Range declared**: 0.5x to 4.0x, 8 points log-spaced | No credible source exists (§17.2) — this is the parameter §17.3's break-even analysis is built around |
| Mandate acceptance rate | Swept | **Range declared**: 0.10 to 0.80, step 0.10 | No credible source exists |
| NSF timing and return latency | Swept | **Range declared**: 1 to 5 business days | No credible source exists |
| Decline distributions (§5.4 out-of-scope items) | Swept | Drawn from `data/failure_taxonomy.yaml`'s sourced codes, frequency **undeclared pending a real distribution** — using a uniform prior over permitted codes as the sweep's null point, explicitly flagged as arbitrary | No credible source exists |
| `promise_credibility_floor` | Structural (this build's own addition, §24.2) | 0.34 | Declared default, not fitted — see DEVDOC_v6 §24.2's amendment |
| Auditor sample rates | Structural (§11.7) | 10% (both jobs) | Declared default, not fitted |
| Number of personas | Structural | **PENDING harness design** — not yet decided | — |
| Window length | Structural | **PENDING harness design** | — |
| Arm assignment ratio | Structural | **PENDING harness design** — 1:1:1:1 (A:B1:B2:C) is the working assumption, not yet committed | — |

## Arm definitions (§17.4)

| Arm | Definition | Status |
|---|---|---|
| **A** | Fixed standard dunning schedule, no model | Buildable now — no LLM dependency. Not yet implemented in `eval/arms/a/`. |
| **B1** | LLM chaser, no policy in the prompt, no gate | Blocked on an LLM API key — no model call exists anywhere in this codebase (see docs/LIMITATIONS.md) |
| **B2** | LLM chaser, the human-readable twin from `agent/bounds/rules.yaml` verbatim in the system prompt, no enforcement gate | Blocked on the same LLM API key. The system-prompt text itself is mechanically extractable right now (every rule's `human:` field, concatenated) — the extraction just has nothing to send it to yet. |
| **C** | TrueCommit — same policy text, enforced by `check_bounds()` | The enforcement side (`check_bounds()`, the full rule register, the differential test) is fully built and tested. The decision side (`DECIDE`, needing a fitted `p_base`) is not — see `agent/decide/ev.py`'s own docstring. |

**If room for only three arms, cut B1, never B2** (§17.4) — noted here so
the harness, once built, doesn't relitigate that call.

## Metric set (§17.7)

Tier 1 metrics that need no eval run are already reported where they're
produced, not deferred to `RESULTS.md`:

- Rail conformance (`agent/rails/conformance/suite.py`, passing against both rails)
- Idempotency (shuffled-thrice, `tests/agent/test_shuffled_replay.py`)
- `check_bounds` twin agreement (5,000-input differential test, `tests/agent/test_bounds_differential.py`)
- Ledger replay + tamper detection (`docs/LEDGER.md`, generated with real output)
- Injection resistance (`tests/agent/test_injection_resistance.py`, 80 tests — structural, not empirical; see docs/THREAT_MODEL.md)

Metrics that need an eval run and are **not yet available**: extraction
field-level F1, family classification macro-F1, objection-veto recall with
Wilson intervals, `p_base` calibration, decision flip rate under `lift`
perturbation (the *mechanism* for this exists — `agent.decide.ev
.decision_flips_under_perturbation` — but needs real `p_base`/`lift` pairs
from a running arm to report anything), autonomy rate, cost per rupee
recovered, abandonment by invoice-size decile, simulator response-rate
error bar (§27).

## Primary comparison

Not yet committed — this is exactly the kind of choice §17.6 exists to
lock in *before* seeing a result, and locking it in before the harness even
exists would be locking in nothing. **This section will be filled in, and
this file re-committed with a fresh hash, before the first arm runs — never
after.**
