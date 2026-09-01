# Results — the synthetic Monte Carlo comparison (DEVDOC_v6 §17)

> **Note.** Recovery in this report is a modelling convention — the harness's own ground truth, not Law 7's rail-confirmed-capture standard. Read these arms as a measurement of bounded execution, not of money.
>
> Separately, and not reflected in any number below: a single real capture has been attributed under Law 7's actual standard by the deployed service (`docs/evidence/REAL_RECOVERY.md`). That is n=1, and it closes the "no rupee has ever crossed the rail" gap rather than the "these arms are synthetic" one.

I generate this with `eval/report.py`, run against parameters I locked in `eval/PREREGISTRATION.md` at commit `1f3b50346bc76400d7e443cc7d69c59c3a1172f4` — every number below comes from that exact, committed-before-running configuration, not a draw I picked after seeing an outcome.

## Section 0 — what this is, and what it is not (§16, §17.1)

I built this as a **synthetic Monte Carlo comparison**: a reproducible population of 500 personas (`eval/personas/generator.py`, seed=42), with a real, fitted `p_base` per persona and a known (synthetic) ground-truth blocker, run through three arms. Arm C is the only arm that calls the *actual* project code (`agent.decide.ev.compute_ev`, `agent.bounds.engine.check_bounds`) rather than a hand-rolled stand-in — I use this to measure whether my real pipeline's logic helps a population with known ground truth, not whether real extraction is accurate (that needs a real golden set, which I haven't built yet — see `docs/LIMITATIONS.md`) and not whether real debtors respond the way this population's declared-prior resolution probabilities assume. I count a rupee here as 'recovered' if a persona's synthetic resolution draw succeeded within the window — a modelling convention for my harness, not Law 7's real rail-confirmed-capture standard, which governs actual money in `agent/ledger/recovery.py`.

## Primary comparison: recovered fraction, Arm A vs Arm C (n=500, seed=42, window=30d, touch_cost=Rs 5, lift_prior=1.0 neutral)

I locked this *because* it needs no assumed behavioural uplift to be interesting: at `lift_prior=1.0`, any Arm C advantage comes from correct diagnosis routing and bounds discipline alone, never from a favourable guess about the one parameter with no empirical source (§17.1).

| Arm | n | Recovered | Resolved | Avg days to resolution | Human escalation | Contact-exhausted ("lost") | Mean touches | **Bounds violations** |
|---|---|---|---|---|---|---|---|---|
| A | 500 | 77.6% | 77.2% | 5.4 | 0.0% | 8.0% | 2.18 | **399** |
| B2 | 500 | 96.2% | 96.4% | 7.1 | 0.0% | 3.2% | 1.89 | **250** |
| C | 500 | 98.4% | 98.4% | 6.7 | 9.6% | 0.4% | 1.78 | **0** |

**Result**: Arm C recovers 98.4% vs Arm A's 77.6% and Arm B2's 96.2% — the highest of the three, **and it does this with 0 real bounds-rule violations against 399 for Arm A and 250 for Arm B2** — every one of those a real, triggerable `DISPUTE_FREEZE` refusal (a plain collection touch against a genuinely disputed persona), not a hypothetical. My harness's Arm B2 is not a fully unbounded chaser (it already respects each persona's own contact-tolerance opt-out threshold, same as Arm A) — a literally unbounded bot would likely out-recover Arm C on raw rupees the way DEVDOC_v6 §17.4 anticipates; my harness's more conservative B2 does not, and I report that honestly rather than adjust it to match the anticipated shape.

## Family B breakout — the identification argument (§17.7, §26.1)

The administrative-blocker (`Blocker.ADMINISTRATIVE`) subpopulation only. **Caveat, stated plainly**: I model the Family-B advantage in my harness through differential diagnostic-accuracy/matching rates (Arm A always 'mismatched' since it never diagnoses; Arm B2/C match at the same declared `DIAGNOSTIC_ACCURACY`), not through literally distinct artifact-repair action mechanics — I do **not** yet fully model DEVDOC_v6 §26.1's stronger claim that the control arm's action *set* structurally lacks a repair action. What's below is a real, honest cut of the real simulation output; it is a weaker form of the identification argument than §26.1 describes, not the full version.

| Arm | n | Recovered | Resolved | Avg days to resolution | Human escalation | Contact-exhausted ("lost") | Mean touches | **Bounds violations** |
|---|---|---|---|---|---|---|---|---|
| A | 2 | 0.0% | 0.0% | n/a | 0.0% | 0.0% | 4.00 | **0** |
| B2 | 2 | 100.0% | 100.0% | 10.5 | 0.0% | 0.0% | 2.50 | **0** |
| C | 2 | 100.0% | 100.0% | 10.5 | 50.0% | 0.0% | 2.50 | **0** |

**Low-power warning, stated rather than hidden**: only 2 of the 500 locked personas landed in the administrative-blocker subpopulation — a direct, honest consequence of the fitted `p_base` model's own high base rate (§17.7/`docs/LIMITATIONS.md`: ~97.9% resolve on their own in the underlying Kaggle data, so few personas ever reach the 'won't resolve without a specific blocker' branch that gets split across blocker types at all). At n=2 I don't treat this table as a reliable estimate of anything — I report it for completeness, per §17.7's own instruction to break Family B out, not as a finding. A population large enough for a statistically meaningful Family-B-only comparison (likely n=5,000+, given how thin this slice is) is future work for me, not something the locked n=500 primary run can retroactively provide without re-locking the pre-registration.

## Lift sensitivity and break-even τ (§17.3)

**At the primary touch cost (Rs 5)**: **No break-even τ exists within the declared 0.5x-4.0x range**: Arm C's recovered fraction meets or exceeds Arm A's at *every* swept point, including the lowest (0.5x) — my honest reading is that **`lift_prior` is not load-bearing** for this comparison at realistic messaging costs; the outcome is driven by diagnosis routing and bounds discipline, not by the swept parameter. `EV_FLOOR` essentially never refuses at this cost against a ~Rs 50,000-median population (recoverable_paise dwarfs a five-rupee touch cost regardless of lift).

| lift | A recovered | B2 recovered | C recovered | C violations |
|---|---|---|---|---|
| 0.5 | 77.6% | 96.2% | 98.4% | 0 |
| 0.673 | 77.6% | 96.2% | 98.4% | 0 |
| 0.906 | 77.6% | 96.2% | 98.4% | 0 |
| 1.219 | 77.6% | 96.2% | 98.4% | 0 |
| 1.641 | 77.6% | 96.2% | 98.4% | 0 |
| 2.208 | 77.6% | 96.2% | 98.4% | 0 |
| 2.972 | 77.6% | 96.2% | 98.4% | 0 |
| 4.0 | 77.6% | 96.2% | 98.4% | 0 |

**Stress test at an elevated touch cost (Rs 20,000)** — I deliberately raised this so `EV_FLOOR` actually binds, to show where the prior *would* become load-bearing: Break-even τ ≈ **0.49** under this artificially harsh cost assumption.

| lift | A recovered | C recovered | C mean touches |
|---|---|---|---|
| 0.5 | 77.6% | 79.4% | 1.28 |
| 0.673 | 77.6% | 93.7% | 1.61 |
| 0.906 | 77.6% | 96.9% | 1.72 |
| 1.219 | 77.6% | 97.6% | 1.74 |
| 1.641 | 77.6% | 98.3% | 1.77 |
| 2.208 | 77.6% | 98.4% | 1.78 |
| 2.972 | 77.6% | 98.4% | 1.78 |
| 4.0 | 77.6% | 98.4% | 1.78 |

## Decision flip rate under ±50% perturbation (§17.5)

I recompute per-persona `EV_FLOOR` pass/refuse, with `lift_prior` perturbed ±50%, over the full real population (n=500).

| Touch cost | Flip rate at 0.5x | Flip rate at 1.5x |
|---|---|---|
| Rs 5 (primary) | 0.0% | 0.0% |
| Rs 20,000 (stress) | 25.8% | 1.4% |

At the primary cost, a 0% flip rate is the same finding as the lift-sweep table above, from an independent angle: `lift_prior` doesn't decide anything at realistic messaging costs. At the stress cost, a nonzero flip rate is expected — this is close to the break-even region I found above, exactly where a ±50% swing in an unsourced parameter should matter most.

## Autonomy rate and unit economics (§25)

§25.1's own concern, which I state directly here: **"bounded" must not become a euphemism for "punts everything to a human."** At the primary comparison's parameters:

| Arm | Autonomy rate (cases closed with zero human touch) | Cost per Rs recovered | Human-minutes per recovery |
|---|---|---|---|
| A | 100.0% | Rs 0.0003 | 0.00 |
| B2 | 100.0% | Rs 0.0002 | 0.00 |
| C | 90.4% | Rs 0.0002 | 0.78 |

`human_minutes_per_recovery` rests on a declared, named assumption I make (`ASSUMED_MINUTES_PER_ESCALATION = 8` minutes per escalated case — no dataset gives me a real human-agent handling time for this, same honesty standard as every other `ASSUMED_*` constant I use in `eval/personas/generator.py`). `cost_per_rupee_recovered` uses the same `touch_cost_paise` (Rs 5) I already name throughout this report and the Rs 50,000 median-invoice scale I already assume in `eval/personas/generator.py` for absolute rupee amounts.

**Arm C's autonomy rate (90.4%) is deliberately lower than the two ungated arms' (100%), and I don't consider that a flaw**: those two arms have no mechanism to escalate anything, ever — their 100% autonomy is the absence of a safety net, not evidence of a more capable system. Arm C's escalations are exactly the disputed-invoice cases §14.4/§24.2's own design says must reach a human; a lower autonomy rate here is the gate working as specified, the same point the violations column makes from a different angle.

## What this is not (repeating docs/LIMITATIONS.md, on purpose)

Not a real-debtor result. Not extraction accuracy (I haven't built a golden set yet). Not a claim about real Indian B2B response rates to any channel or instrument — `lift_prior` and the resolution-probability tables in `eval/simulate.py` are priors I declare with no empirical source (§17.1), exactly the parameter I built this report's own sensitivity analysis to interrogate rather than assume.
