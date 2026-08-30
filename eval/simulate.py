#!/usr/bin/env python3
"""Monte Carlo comparison of Arms A / B2 / C over a synthetic population.
DEVDOC_v6 §17.1-§17.4.

Zero real model calls: this measures the *deterministic pipeline's*
aggregate effect (does routing through Diagnose/Decide/Bounds actually
produce a better outcome than a fixed schedule or an ungated chaser), which
needs no LLM at all — the same principle `tests/agent/test_injection_
resistance.py` already uses (a mocked/simulated diagnosis exercises real
downstream logic). "Diagnostic accuracy" below stands in for what a real
extractor would get right or wrong; it is not attempting to simulate what
any specific model would say.

Arm B1 (LLM chaser, no policy, no gate) is not implemented here —
`eval/PREREGISTRATION.md` already commits to cutting B1 before B2 if only
three arms fit, so this harness builds exactly the three that matter most:
A (the control), B2 (policy known, unenforced), C (policy enforced).

Every constant below is FITTED (traces to data/fitted_params.yaml) or
ASSUMED (a declared Prior — no dataset gives Indian B2B AR intervention
response, DEVDOC_v6 §17.1). Arm C is the only arm that calls real
project code (`compute_ev`, `check_bounds`, `ACTIONS_UNLOCKED`) rather than
a hand-rolled stand-in — that's deliberate: the whole point of this harness
is to measure what the *actual* gate and EV logic do in aggregate, not a
reimplementation of them.

    uv run python eval/simulate.py --n 300 --seed 1 --lift 2.0

Numbers below are harness defaults for exercising the code — NOT a
pre-registered run. DEVDOC_v6 §17.6 requires locking in population size,
window length, and the primary comparison metric *before* the first real
run counts as evidence; that commitment belongs in `eval/PREREGISTRATION.md`
in its own commit, made before anyone reads a result, not in this file's
defaults.
"""

from __future__ import annotations

import argparse
import random
import statistics
from dataclasses import dataclass
from enum import Enum

from agent.bounds.context import ActionCtx, BoundsContext, ConfigCtx, DebtorCtx, DecisionCtx, InvoiceCtx, MandateCtx
from agent.bounds.engine import check_bounds
from agent.decide.ev import Prior, compute_ev
from agent.decide.fitted_p_base import load_fitted_p_base
from agent.diagnose.extract import ACTIONS_UNLOCKED, Family
from eval.arms.a.schedule import touch_for_day
from eval.personas.generator import Blocker, Persona, generate_population

CHECKIN_DAYS: tuple[int, ...] = (3, 8, 13, 18, 23, 28)
"""Arm B2 and Arm C's check-in cadence -- more frequent than Arm A's fixed
(1, 7, 14, 21), representing a chaser (model-driven or gated) that looks in
on a case more often than a static schedule would. A structural choice
for this harness, not fitted."""

DEFAULT_TOUCH_COST_PAISE = 500
"""Rs 5 nominal cost per touch, for compute_ev()'s cost_paise term -- a
structural placeholder (a message/call has *some* cost), not fitted.

**Honest finding from running this harness**: at this cost against a
~Rs 50,000-median invoice population, EV_FLOOR essentially never refuses
anywhere in the declared lift sweep range (0.5x-4.0x, §17.2) --
recoverable_paise dwarfs a five-rupee touch cost regardless of lift, so
`--lift` has almost no visible effect on the printed summary at the
default cost. That's a real property of this population (a message is
cheap relative to a large B2B invoice), not a bug in the sweep -- EV_FLOOR
matters more for low-value invoices or costlier actions (a human's time on
an escalation, not modelled here). `--touch-cost-paise` is exposed so this
can actually be explored instead of silently not mattering."""

DIAGNOSTIC_ACCURACY = Prior(0.85)
"""P(Arm C's simulated Diagnose stage identifies the persona's true blocker
correctly). Stands in for extractor accuracy pending a real golden set
(§17.8) -- an assumed, round number, not measured."""

ASSUMED_RESOLUTION_PROB_MATCHED = Prior({
    Blocker.NONE: 0.55, Blocker.INSTRUMENT: 0.35, Blocker.ADMINISTRATIVE: 0.45, Blocker.DISPUTE: 0.50,
})
"""Per-checkin probability of resolving THIS period when the action taken
actually matches the persona's true blocker (Blocker.NONE has no
"mismatch" concept -- any reasonable contact is equally fine, so matched
and mismatched are equal for it). No dataset gives this number; kept in the
same coin-flip-ish neighborhood across families rather than tuned to
produce a desired comparison."""

ASSUMED_RESOLUTION_PROB_MISMATCHED = Prior({
    Blocker.NONE: 0.55, Blocker.INSTRUMENT: 0.05, Blocker.ADMINISTRATIVE: 0.10, Blocker.DISPUTE: 0.03,
})
"""Same, when the action does NOT match the true blocker -- a generic
reminder rarely fixes an instrument failure or a live dispute."""

ASSUMED_HUMAN_ESCALATION_RESOLUTION_PROB = Prior(0.70)
"""P(a human who picks up an escalated case resolves it) -- a declared
prior representing "people have tools this simulation doesn't model", not
a measured human-agent success rate."""

_PREFERRED_ACTION_BY_FAMILY: dict[Family, str] = {
    Family.A: "retry_charge", Family.B: "reissue_artifact",
    Family.C: "send_reminder", Family.D: "escalate_human",
}
for _family, _action in _PREFERRED_ACTION_BY_FAMILY.items():
    assert _action in ACTIONS_UNLOCKED[_family], (
        f"{_action!r} is not in ACTIONS_UNLOCKED[{_family!r}] -- eval/simulate.py's "
        "action choice has drifted from agent.diagnose.extract's real table"
    )

_BLOCKER_TO_FAMILY: dict[Blocker, Family] = {
    Blocker.NONE: Family.C, Blocker.INSTRUMENT: Family.A,
    Blocker.ADMINISTRATIVE: Family.B, Blocker.DISPUTE: Family.D,
}


@dataclass(frozen=True, slots=True)
class Outcome:
    persona_id: str
    amount_paise: int
    resolved_day: int | None
    recovered_paise: int
    escalated_to_human: bool
    contact_exhausted: bool
    touches: int


@dataclass(frozen=True, slots=True)
class ArmSummary:
    arm: str
    n: int
    recovered_fraction: float
    """sum(recovered_paise) / sum(amount_paise) across the population --
    simplified to full-amount recovery on resolution (not the undisputed-
    portion-only nuance select_instrument() applies for a real dispute);
    a known simplification for this first version of the harness."""
    resolved_fraction: float
    mean_days_to_resolution: float | None
    """None if nobody in this arm resolved within the window."""
    human_escalation_rate: float
    contact_exhausted_rate: float
    mean_touches: float


def _resolution_prob(true_blocker: Blocker, matched: bool) -> float:
    table = ASSUMED_RESOLUTION_PROB_MATCHED.value if matched else ASSUMED_RESOLUTION_PROB_MISMATCHED.value
    return table[true_blocker]


def run_arm_a(personas: list[Persona], *, window_days: int, rng: random.Random) -> list[Outcome]:
    """Fixed schedule (eval/arms/a/schedule.py's real touch_for_day), blind
    to the true blocker -- every touch is treated as "mismatched" since Arm
    A never diagnoses anything. No bounds gate: nothing stops it contacting
    a persona past their tolerance, which is exactly what produces this
    arm's contact-exhausted ("lost") personas."""
    outcomes = []
    for p in personas:
        touches_on_channel = 0
        opted_out = False
        resolved_day: int | None = None
        touches = 0
        for day in range(1, window_days + 1):
            if opted_out or resolved_day is not None:
                break
            if touch_for_day(day) is None:
                continue
            touches += 1
            touches_on_channel += 1
            if rng.random() < _resolution_prob(p.true_blocker, matched=(p.true_blocker is Blocker.NONE)):
                resolved_day = day
                break
            if touches_on_channel >= p.contact_tolerance:
                opted_out = True
        outcomes.append(Outcome(
            persona_id=p.id, amount_paise=p.amount_paise, resolved_day=resolved_day,
            recovered_paise=p.amount_paise if resolved_day is not None else 0,
            escalated_to_human=False, contact_exhausted=opted_out and resolved_day is None,
            touches=touches,
        ))
    return outcomes


def run_arm_b2(personas: list[Persona], *, window_days: int, rng: random.Random) -> list[Outcome]:
    """LLM chaser with the human-readable policy text available to it, but
    nothing enforcing it (PREREGISTRATION.md's Arm B2). Modelled as:
    sometimes picks the action that actually matches the true blocker
    (it knows the policy) at the same rate as Arm C's diagnostic accuracy,
    but has no bounds gate -- so, like Arm A, nothing stops it contacting a
    persona past their tolerance, and it checks in more often than Arm A's
    static schedule."""
    outcomes = []
    for p in personas:
        touches_on_channel = 0
        opted_out = False
        resolved_day: int | None = None
        touches = 0
        for day in CHECKIN_DAYS:
            if day > window_days or opted_out or resolved_day is not None:
                break
            touches += 1
            touches_on_channel += 1
            matched = rng.random() < DIAGNOSTIC_ACCURACY.value
            if rng.random() < _resolution_prob(p.true_blocker, matched=matched):
                resolved_day = day
                break
            if touches_on_channel >= p.contact_tolerance:
                opted_out = True
        outcomes.append(Outcome(
            persona_id=p.id, amount_paise=p.amount_paise, resolved_day=resolved_day,
            recovered_paise=p.amount_paise if resolved_day is not None else 0,
            escalated_to_human=False, contact_exhausted=opted_out and resolved_day is None,
            touches=touches,
        ))
    return outcomes


def run_arm_c(
    personas: list[Persona], *, window_days: int, rng: random.Random, lift_prior: "Prior[float]",
    touch_cost_paise: int = DEFAULT_TOUCH_COST_PAISE,
) -> list[Outcome]:
    """The full pipeline's decision logic, for real: compute_ev() and
    check_bounds() are the actual project functions, not stand-ins. Only
    Diagnose is simulated (there's no real text to extract from a synthetic
    persona) -- everything downstream of a diagnosis runs the real code."""
    outcomes = []
    for p in personas:
        touches_on_channel = 0
        opted_out_channels: set[str] = set()
        resolved_day: int | None = None
        escalated = False
        touches = 0

        for day in CHECKIN_DAYS:
            if day > window_days or resolved_day is not None:
                break

            matched = rng.random() < DIAGNOSTIC_ACCURACY.value
            diagnosed = p.true_blocker if matched else rng.choice(
                [b for b in Blocker if b != p.true_blocker]
            )
            family = _BLOCKER_TO_FAMILY[diagnosed]
            action_type = _PREFERRED_ACTION_BY_FAMILY[family]

            decision = compute_ev(
                p_base=p.p_base, lift_prior=lift_prior, recoverable_paise=p.amount_paise,
                cost_paise=touch_cost_paise, action_type=action_type,
            )
            if decision.ev_paise <= 0:
                continue  # EV_FLOOR would refuse this touch -- skip to the next check-in

            ctx = BoundsContext(
                debtor=DebtorCtx(id=p.id, state="ENGAGED", touches_7d=touches_on_channel,
                                  opted_out_channels=frozenset(opted_out_channels)),
                mandate=MandateCtx(),
                action=ActionCtx(type=action_type, channel="telegram", rail_tag="simulated"),
                decision=DecisionCtx(ev_paise=decision.ev_paise),
                invoice=InvoiceCtx(id=f"inv_{p.id}", recovery_attempts=touches),
                config=ConfigCtx(),
            )
            result = check_bounds(ctx)
            touches += 1

            if not result.passed:
                escalated = True
                if rng.random() < ASSUMED_HUMAN_ESCALATION_RESOLUTION_PROB.value:
                    resolved_day = day
                break

            touches_on_channel += 1
            if rng.random() < _resolution_prob(p.true_blocker, matched=matched):
                resolved_day = day
                break
            if touches_on_channel >= p.contact_tolerance:
                opted_out_channels.add("telegram")
                touches_on_channel = 0

        outcomes.append(Outcome(
            persona_id=p.id, amount_paise=p.amount_paise, resolved_day=resolved_day,
            recovered_paise=p.amount_paise if resolved_day is not None else 0,
            escalated_to_human=escalated, contact_exhausted=bool(opted_out_channels) and resolved_day is None,
            touches=touches,
        ))
    return outcomes


def summarize(arm: str, outcomes: list[Outcome]) -> ArmSummary:
    n = len(outcomes)
    resolved = [o for o in outcomes if o.resolved_day is not None]
    total_amount = sum(o.amount_paise for o in outcomes)
    total_recovered = sum(o.recovered_paise for o in outcomes)
    return ArmSummary(
        arm=arm, n=n,
        recovered_fraction=(total_recovered / total_amount) if total_amount else 0.0,
        resolved_fraction=len(resolved) / n if n else 0.0,
        mean_days_to_resolution=statistics.mean(o.resolved_day for o in resolved) if resolved else None,
        human_escalation_rate=sum(o.escalated_to_human for o in outcomes) / n if n else 0.0,
        contact_exhausted_rate=sum(o.contact_exhausted for o in outcomes) / n if n else 0.0,
        mean_touches=statistics.mean(o.touches for o in outcomes) if outcomes else 0.0,
    )


def run_comparison(
    *, n_personas: int, seed: int, window_days: int, lift: float,
    touch_cost_paise: int = DEFAULT_TOUCH_COST_PAISE,
) -> dict[str, ArmSummary]:
    """The same population, same seed, run through all three arms -- the
    fairness property §17.6 pre-registration exists to protect: nobody's
    strategy gets easier cases than anyone else's."""
    p_base_model = load_fitted_p_base()
    personas = generate_population(n_personas, seed=seed, p_base_model=p_base_model)
    lift_prior = Prior(lift)

    return {
        "A": summarize("A", run_arm_a(personas, window_days=window_days, rng=random.Random(seed))),
        "B2": summarize("B2", run_arm_b2(personas, window_days=window_days, rng=random.Random(seed))),
        "C": summarize("C", run_arm_c(
            personas, window_days=window_days, rng=random.Random(seed),
            lift_prior=lift_prior, touch_cost_paise=touch_cost_paise,
        )),
    }


def _print_summary(summaries: dict[str, ArmSummary]) -> None:
    print(f"{'Arm':<4} {'n':>5} {'Recovered':>10} {'Resolved':>9} {'Avg days':>9} {'Human esc':>10} {'Lost':>6} {'Touches':>8}")
    for arm, s in summaries.items():
        avg_days = f"{s.mean_days_to_resolution:.1f}" if s.mean_days_to_resolution is not None else "n/a"
        print(
            f"{arm:<4} {s.n:>5} {s.recovered_fraction:>9.1%} {s.resolved_fraction:>9.1%} "
            f"{avg_days:>9} {s.human_escalation_rate:>9.1%} {s.contact_exhausted_rate:>6.1%} {s.mean_touches:>8.2f}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Monte Carlo comparison of Arms A/B2/C -- see this file's module docstring."
    )
    parser.add_argument("--n", type=int, default=300, help="population size (harness default, not pre-registered)")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--lift", type=float, default=2.0, help="lift_prior value (§17.2 sweep range: 0.5-4.0)")
    parser.add_argument(
        "--touch-cost-paise", type=int, default=DEFAULT_TOUCH_COST_PAISE,
        help="EV cost per touch in paise -- at the default (Rs 5), EV_FLOOR essentially never "
             "refuses against this population; raise this (e.g. to Rs 20,000+) to see --lift "
             "actually change Arm C's outcome. See DEFAULT_TOUCH_COST_PAISE's docstring.",
    )
    args = parser.parse_args(argv)

    print("Not a pre-registered run -- see eval/PREREGISTRATION.md and this file's module docstring.")
    print(f"n={args.n} seed={args.seed} window_days={args.window_days} lift={args.lift} touch_cost_paise={args.touch_cost_paise}\n")
    summaries = run_comparison(
        n_personas=args.n, seed=args.seed, window_days=args.window_days,
        lift=args.lift, touch_cost_paise=args.touch_cost_paise,
    )
    _print_summary(summaries)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
