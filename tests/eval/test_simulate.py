"""eval/simulate.py -- reproducibility and the structural invariants this
harness exists to demonstrate (Arm C's real check_bounds()/compute_ev()
calls should produce fewer contact-exhausted debtors and a nonzero human
escalation rate, unlike the two ungated arms)."""

from __future__ import annotations

from agent.decide.ev import Prior
from eval.simulate import (
    DEFAULT_TOUCH_COST_PAISE,
    Outcome,
    run_arm_a,
    run_arm_b2,
    run_arm_c,
    run_comparison,
    summarize,
)
from eval.personas.generator import generate_population
import random


class TestRunComparisonReproducibility:
    def test_same_inputs_are_fully_reproducible(self):
        a = run_comparison(n_personas=200, seed=11, window_days=30, lift=2.0)
        b = run_comparison(n_personas=200, seed=11, window_days=30, lift=2.0)
        assert a == b

    def test_different_seeds_differ(self):
        a = run_comparison(n_personas=200, seed=11, window_days=30, lift=2.0)
        b = run_comparison(n_personas=200, seed=12, window_days=30, lift=2.0)
        assert a != b


class TestStructuralInvariants:
    """These are the properties the whole harness exists to check for --
    if any of these ever flip, something in the real check_bounds()/
    compute_ev() wiring (not just the outcome-probability constants) has
    changed in a way worth noticing."""

    def test_arm_a_never_escalates_to_a_human(self):
        personas = generate_population(300, seed=20)
        outcomes = run_arm_a(personas, window_days=30, rng=random.Random(20))
        assert all(o.escalated_to_human is False for o in outcomes)

    def test_arm_b2_never_escalates_to_a_human(self):
        personas = generate_population(300, seed=21)
        outcomes = run_arm_b2(personas, window_days=30, rng=random.Random(21))
        assert all(o.escalated_to_human is False for o in outcomes)

    def test_arm_c_escalates_some_cases_a_and_b2_structurally_cannot(self):
        """Arm C is the only arm wired to a real check_bounds() call, so it's
        the only one that can ever route to a human via CHANNEL_EXHAUSTION
        or similar -- this is the mechanism, not a tuned outcome."""
        personas = generate_population(400, seed=22)
        outcomes = run_arm_c(personas, window_days=30, rng=random.Random(22), lift_prior=Prior(2.0))
        assert any(o.escalated_to_human for o in outcomes)

    def test_arm_c_loses_fewer_debtors_to_contact_exhaustion_than_arm_a(self):
        """The core thesis, structurally: Arm C's bounds gate redirects an
        about-to-be-exhausted debtor to a human instead of letting the
        channel run out -- Arm A has no such gate. Checked at n=600 to keep
        sampling noise well below the gap this produces."""
        summaries = run_comparison(n_personas=600, seed=23, window_days=30, lift=2.0)
        assert summaries["C"].contact_exhausted_rate < summaries["A"].contact_exhausted_rate

    def test_ev_floor_actually_refuses_when_cost_dominates_recoverable_amount(self):
        """Regression test for a real finding made while building this
        harness: at the Rs-5 default touch cost, EV_FLOOR essentially never
        refuses against a ~Rs 50,000-median population (see
        DEFAULT_TOUCH_COST_PAISE's docstring) -- so this only shows up at a
        deliberately large touch cost, confirming the mechanism is wired
        correctly rather than silently dead."""
        personas = generate_population(300, seed=24)
        cheap = run_arm_c(personas, window_days=30, rng=random.Random(24), lift_prior=Prior(4.0),
                           touch_cost_paise=DEFAULT_TOUCH_COST_PAISE)
        expensive = run_arm_c(personas, window_days=30, rng=random.Random(24), lift_prior=Prior(0.5),
                               touch_cost_paise=45_000_00)
        assert summarize("cheap", cheap).mean_touches > summarize("expensive", expensive).mean_touches
        assert summarize("expensive", expensive).mean_touches < 0.5


class TestSummarize:
    def test_empty_outcomes_does_not_crash(self):
        result = summarize("X", [])
        assert result.n == 0
        assert result.recovered_fraction == 0.0
        assert result.mean_days_to_resolution is None

    def test_recovered_fraction_is_amount_weighted_not_a_head_count(self):
        outcomes = [
            Outcome(persona_id="a", amount_paise=100, resolved_day=1, recovered_paise=100,
                    escalated_to_human=False, contact_exhausted=False, touches=1),
            Outcome(persona_id="b", amount_paise=900, resolved_day=None, recovered_paise=0,
                    escalated_to_human=False, contact_exhausted=True, touches=3),
        ]
        result = summarize("X", outcomes)
        assert result.recovered_fraction == 0.1  # 100 / 1000, not 1/2
        assert result.resolved_fraction == 0.5   # head count, by design -- a different metric
