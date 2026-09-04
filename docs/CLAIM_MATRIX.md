# Claim matrix

Every quantitative claim I make, mapped to the command that regenerates it
and the test that fails if it drifts.

I wrote this because "the numbers are reproducible" is itself a claim, and
it's one a reader shouldn't have to take on trust. If a figure below can't
be traced to a command and a guard, it shouldn't be in my README.

**How to read a row.** *Regenerate* is what you run to produce the number
yourself. *Guarded by* is the test that fails if the published figure and
the real one disagree — which is the difference between a number that's
reproducible and a number that's merely *reproduced once*. Rows marked
`no automated guard` are honest about not having one.

Everything here assumes `uv sync` and a clean clone. Rows needing
credentials say so.

---

## Live rail — real money

| Claim | Value | Regenerate | Guarded by |
|---|---|---|---|
| Agent-chosen decisions on the live rail | 10 | `uv run python tools/run_real_batch.py --n 10` *(needs Razorpay test keys; creates real objects)* | no automated guard — a live run costs real API calls, so CI can't re-derive it |
| Real invoices created | 5 | same | `docs/evidence/real_batch_state.json` records every `inv_*` id |
| Invoices paid, **status fetched back from Razorpay** | 5 of 5 | `uv run python tools/report_real_batch.py` | re-fetches live on every run — a stale claim self-corrects |
| Value captured | ₹215,867 | same | sum of `amount_paid` from the fetch, not from my ledger |
| Ledger hash chain verified across the batch | true | `Ledger.verify_chain()` inside the tool | `tests/agent/test_ledger*.py` |
| Rail errors, and why | 5 (Razorpay invoice-creation rate limit) | `docs/evidence/REAL_BATCH.md` | `tests/test_real_batch_rate_limiting.py` — 11 tests on the retry/spacing fix |

**The verification method is the claim.** Every status is re-fetched from
Razorpay at report time rather than read out of my own ledger. My ledger
says what the agent believed; the fetch says what Razorpay holds.

---

## Evaluation — simulated, pre-registered

Locked in [`eval/PREREGISTRATION.md`](../eval/PREREGISTRATION.md) at its own
commit **before** the run: n=500, seed=42, window=30d, lift_prior=1.0.

| Claim | Value | Regenerate | Guarded by |
|---|---|---|---|
| Arm A recovered fraction | 77.6% | `uv run python eval/report.py` | `tests/test_generated_docs_are_current.py::test_results_md_matches_what_the_eval_actually_produces` |
| Arm B2 recovered fraction | 96.2% | same | same |
| Arm C recovered fraction | 98.4% | same | same |
| Bounds violations, A / B2 / C | 399 / 250 / **0** | same | same |
| Arm C vs B2 significance | +2.0 pp, p = 0.0469 | same | `tests/eval/test_stats.py::TestTheDifferenceBetweenArms::test_the_gap_that_actually_needed_testing` |
| Wilson intervals on every arm rate | published | `eval/stats.py` | `tests/eval/test_stats.py` — checked against Newcombe's published reference table *and* against the interval's defining quadratic |
| `lift_prior` sweep | not load-bearing at any swept point | `uv run python eval/report.py` | doc-staleness gate above |
| Family-B second run | separate pre-registration | `uv run python eval/report_family_b.py` | `tests/test_generated_docs_are_current.py::test_results_family_b_matches_what_its_eval_produces` |

**These are simulated.** Synthetic personas with declared resolution
probabilities. They measure whether my pipeline's *judgment* helps a
population with known ground truth. They are not evidence about real
debtors, and `RESULTS.md` says so in its first paragraph.

---

## Extraction accuracy — pre-registered golden set

Labels committed in `fa46965` **before** the extractor ran against them.
`score.py` refuses to run if the label file has uncommitted changes, and
refuses on a shallow clone rather than passing for the wrong reason.

| Claim | Value | Regenerate | Guarded by |
|---|---|---|---|
| Class accuracy, extractor | 49/50 | `uv run python -m eval.golden.score --extractor` *(needs `ANTHROPIC_API_KEY`; ~50 calls)* | `tests/test_generated_docs_are_current.py::test_extraction_accuracy_matches_what_the_scorer_produces` |
| Class accuracy, keyword baseline | 45/50 | `uv run python -m eval.golden.score --baseline` *(free, no API)* | same |
| Family accuracy, extractor / baseline | 50/50 vs 46/50 | same | same |
| **Class gap is NOT significant** | +8.0 pp, p = 0.092 | same | `tests/eval/test_stats.py` |
| Family gap is significant | +8.0 pp, p = 0.041 | same | same |
| Pre-registration ordering holds | labels precede results | `git log --format='%H %ci' -- eval/golden/replies.jsonl` | `tests/test_generated_docs_are_current.py::test_the_golden_labels_are_committed_before_the_results_they_score` |
| Golden set integrity | 50 items, all labels valid family/class pairs | — | `tests/eval/test_golden_set.py` — 165 tests |
| Baseline is a real attempt, not a strawman | ≥50% family accuracy | — | `tests/eval/test_golden_set.py::TestTheBaseline::test_it_is_a_real_attempt_not_a_strawman` |
| Baseline does not solve the task | <100% class accuracy | — | `tests/eval/test_golden_set.py::TestTheBaseline::test_it_does_not_solve_the_task` |

---

## The bounds gate

| Claim | Value | Regenerate | Guarded by |
|---|---|---|---|
| Rules in the register | 20 (7 regulatory, 13 stopping) | `agent/bounds/rules.yaml` | `tests/agent/test_bounds_differential.py::test_every_rule_in_the_register_has_a_human_twin` |
| Machine rule and hand-written twin agree | 5,000 generated cases | `uv run pytest tests/agent/test_bounds_differential.py` | `test_machine_rule_and_human_twin_agree` (`max_examples=5000`) |
| `WHATSAPP_SESSION_WINDOW` is `stopping`, not `regulatory` | asserted | — | `tests/regulatory/test_whatsapp_session_window.py` |
| Generated rule table matches the YAML | byte-identical | `uv run python tools/gen_docs.py` | `tests/test_generated_docs_are_current.py` |
| A forged ledger verdict is caught | raises `BoundsIntegrityBreach` | — | `tests/agent/test_auditor.py` |

---

## Diagnosis and safety

| Claim | Value | Regenerate | Guarded by |
|---|---|---|---|
| Diagnosis classes | 29 across 4 families | `agent/diagnose/extract.py` | `tests/eval/test_golden_set.py::TestTheSetItself::test_every_label_is_a_real_family_class_pair` |
| Injection corpus | 40 cases, 8 classes of 5 | `data/injection_corpus.jsonl` | `tests/agent/test_injection_resistance.py::test_corpus_has_forty_entries_across_eight_classes_of_five_each` |
| No corpus entry unlocks a dangerous action | 0 | — | `test_worst_case_extraction_for_every_non_schema_corpus_entry_unlocks_no_dangerous_action` |
| Adversarial personas permanently stalled | **0 of 300 runs** | `uv run python tools/run_adversarial_personas.py` | `docs/evidence/ADVERSARIAL_PERSONAS.md` — no automated staleness gate |
| Dry-run batch decisions | 500, spanning all 29 classes | `uv run python tools/run_dry_run_batch.py --n 500` | the tool itself asserts `rail_links_created == 0` and aborts if not |

---

## Build and process

| Claim | Value | Regenerate | Guarded by |
|---|---|---|---|
| Test suite | 1,465 collected / 1,454 passing / 11 skipped | `uv run pytest` | `tests/test_documented_test_counts.py` — fails the build if README or LIMITATIONS drift from a real collection |
| This matrix's own cited test nodes all exist | asserted | `uv run pytest tests/test_claim_matrix.py` | `tests/test_claim_matrix.py::TestEveryCitedGuardExists::test_every_cited_test_node_is_collectable` |
| Documented defects | 32 | `docs/WHAT_BROKE.md` | no automated guard — the count is prose |
| API spend ceiling | $20 hard, checked before each call | `agent/spend.py` | `tests/agent/test_spend.py` |
| CI runs on a clean clone with no credentials | both jobs green | `.github/workflows/ci.yml` | `fetch-depth: 0` on both jobs — a shallow clone would make the doc gates pass for the wrong reason (`WHAT_BROKE.md` #22) |

---

## Claims with no automated guard

Listing these rather than letting a reader assume everything is gated:

- **₹215,867 captured** and the real-batch row counts. A live run costs
  real API calls, so CI can't re-derive them. The mitigation is that
  `report_real_batch.py` re-fetches from Razorpay every time it runs, so a
  stale figure corrects itself the moment anyone regenerates it.
- **30 entries in `WHAT_BROKE.md`.** Prose, counted by hand.
- **The 300 adversarial-persona runs.** Regenerable, but not gated by a
  `--check` mode the way `RESULTS.md` is.
- **₹91,72,435 at risk.** Regenerable via
  `tools/compute_at_risk_headline.py`, but it is arithmetic over a batch I
  generated with declared breach rates — so the number follows from
  parameters I chose, and `AT_RISK_HEADLINE.md` says so.

---

## Why three docs are byte-gated and the rest aren't

`RESULTS.md`, `RESULTS_FAMILY_B.md` and `EXTRACTION_ACCURACY.md` each have a
`--check` mode that regenerates them and compares byte-for-byte. A test runs
all three.

That exists because `RESULTS.md` silently went stale once
(`WHAT_BROKE.md` #18): behaviour changed underneath it, the committed
numbers drifted, and the headline claims still *held*, which is exactly why
nobody noticed. The lesson recorded there is that gating one results
document and not another is how the first one drifts — so
`EXTRACTION_ACCURACY.md` was gated in the same commit that created it,
before it had a chance to.

None of those three carries a wall-clock timestamp, deliberately: they are
compared byte-for-byte, and the provenance that carries a claim is the
pre-registration commit, not the hour the markdown was written.
