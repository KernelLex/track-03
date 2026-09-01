# Pre-Registration II — a population that can actually test Claim 1

**Committed before the run it governs.** This file exists in its own
commit, made before `eval/report_family_b.py` was written or executed, so
the git history shows the ordering rather than asking anyone to take it on
trust. `docs/RESULTS_FAMILY_B.md` cites this commit's hash.

## Why a second pre-registration exists

The first one (`eval/PREREGISTRATION.md`) is untouched and stays published,
along with `docs/RESULTS.md` exactly as it was generated. Nothing here
replaces it. This is a **power analysis for Claim 1**, run because the
first population could not test the claim the project rests on.

Claim 1 is that diagnosis-first routing beats reminder-based chasing on
**administrative blockers** — invoices stuck behind a wrong GSTIN, a PO
mismatch, a missing document — because no amount of reminding fixes a
defect in the artifact. The identification argument (§26.1) is clean: the
control arm's action set contains no action that removes a blocking
artifact defect, so the comparison is identified by the action sets rather
than by the response model.

**The first population put n=2 in that subpopulation.** Two. Its Family B
row moved from `50.0% escalation, 2.50 touches` to `0.0%, 1.00` when the
eval was regenerated after unrelated fixes — a fifty-point swing from one
case changing. A row that moves fifty points on one case is not a weak
result, it is arithmetically incapable of being a result at all.

**That was a design fault, not bad luck, and no seed change fixes it.** In
`generate_population`:

```python
resolves_on_its_own = (not is_disputed) and rng.random() < p_base
```

`p_base` comes from the fitted model and is high, so most invoices land in
`Blocker.NONE` and only the residual — not disputed, doesn't self-resolve —
splits across the three real blocker types. At n=500 that residual is
single digits. The composition of the population is a *downstream
consequence of a fitted parameter*, which is entirely correct for asking
"what does a realistic portfolio look like" and entirely wrong for asking
"does the intervention work on this blocker type".

Publishing a framing note about n=2 would not have fixed it. Regenerating
the population does.

## What is declared here, and what that costs

| Parameter | Value | Status |
|---|---|---|
| Population size (`n`) | **500** | Structural — same as the first run, for comparability |
| Random seed | **43** | Structural. Deliberately *not* 42: this is a different population and the seed should say so at a glance |
| Window length | **30 days** | Inherited, unchanged — the fitted `p_base` model's own horizon |
| Touch cost | **Rs 5** | Inherited, unchanged |
| `lift_prior` | **1.0 (neutral)** | Inherited, unchanged. Any Arm C advantage must come from routing and bounds discipline, not from an assumed uplift |
| **Blocker mix** | **NONE 0.30, INSTRUMENT 0.20, ADMINISTRATIVE 0.35, DISPUTE 0.15** | **ASSUMED — declared, not fitted.** This is the new parameter and the whole point of this run |

`ADMINISTRATIVE 0.35` at n=500 gives roughly 175 cases, which is enough for
a recovered-fraction difference to mean something.

**What declaring the mix costs, stated plainly.** This population is
*constructed*, not sampled. It cannot tell anyone the real-world rate of
administrative blockers in Indian B2B receivables — nothing in this project
can, and the first population's mix was not evidence of that either, since
it was a consequence of a fitted self-resolution rate applied to a
US-derived amount distribution.

So this run answers a strictly **conditional** question: *given* an
administrative blocker, does the gated diagnosis-first pipeline recover
more than reminder-based chasing? That conditional is what Claim 1 actually
asserts. The unconditional version — how much money this saves a real
business — needs the blocker mix to be measured, and it is not measured
here or anywhere else in this repo.

**`NONE 0.30`** is the second declared choice worth naming. The first
population had ~78% self-resolving, and a population where most invoices
pay themselves cannot test an intervention regardless of what else is
declared. 0.30 keeps a substantial self-resolving share — so Arm C still
has to avoid wasting touches on invoices that need none, which is a real
part of the claim — while leaving enough hard cases to measure.

**`p_base` is still fitted per persona and still drives DECIDE's EV
computation.** Only the assignment of `true_blocker` is declared. The
fitted model is not overridden, it is no longer the thing that decides
composition.

## The primary comparison, fixed now

**Recovered fraction, Arm A vs Arm C, within the administrative
subpopulation only**, at the parameters above.

Reported alongside, not instead of:

- the same comparison across the whole population, so the constructed mix's
  effect on the headline is visible rather than hidden
- real `check_bounds()` violations per arm (expected 0 for C; this run is
  not primarily about that, and it would be dishonest to lead with a metric
  the first run already established)
- mean touches and human-escalation rate per arm within the subpopulation
- n in the administrative subpopulation, stated on the face of the results
  table, so the thing that made the first run uninformative is checkable at
  a glance

## What counts as the claim failing

Fixed before the run, because a threshold chosen afterwards is not a
threshold:

**Claim 1 is not supported by this run if Arm C's recovered fraction within
the administrative subpopulation is not at least 5 percentage points above
Arm A's.**

Five points is a deliberate floor rather than statistical significance: the
identification argument says the control arm *structurally cannot* fix an
artifact defect, so a real effect should be large. A difference smaller
than that would suggest the mechanism is not doing what the argument says
it does, whatever the p-value.

**If that happens, this file and the resulting numbers get published
unchanged, and `docs/LIMITATIONS.md` records that the central claim did not
survive a population built to test it.** No third population, no adjusted
threshold, no additional arm. That commitment is the only thing that makes
the first two sections worth reading, and it is made here, in a commit that
precedes the run.

## What this run deliberately does not do

- **It does not replace `docs/RESULTS.md`.** Different output file
  (`docs/RESULTS_FAMILY_B.md`), different generator entry point. The first
  run's numbers stay exactly as published, and `eval/report.py` still
  regenerates them byte-for-byte.
- **It does not change `generate_population`'s default behaviour.** The
  blocker mix is an optional parameter; omitted, the function does what it
  did before, which is what keeps the first result reproducible.
- **It does not add an arm or a metric** beyond those already defined in
  `eval/PREREGISTRATION.md` §"Metric set".
