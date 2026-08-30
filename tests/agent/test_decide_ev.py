"""DECIDE stage's EV arithmetic, and the type-level p_base/lift_prior split
DEVDOC_v6 §11.4 calls for. Neither is fitted here -- see test names for what's
actually being asserted (arithmetic and typing, not a real prediction)."""

from __future__ import annotations

import pytest

from agent.decide.ev import Decision, InvalidProbability, Prior, compute_ev, decision_flips_under_perturbation, perturb


def test_ev_arithmetic_matches_the_formula_exactly():
    decision = compute_ev(
        p_base=0.5, lift_prior=Prior(2.0), recoverable_paise=100_000, cost_paise=10_000, action_type="create_mandate",
    )
    assert decision.ev_paise == int(0.5 * 2.0 * 100_000) - 10_000


def test_lift_prior_must_be_a_prior_not_a_bare_float():
    with pytest.raises(TypeError):
        compute_ev(p_base=0.5, lift_prior=2.0, recoverable_paise=1000, cost_paise=100, action_type="x")  # type: ignore[arg-type]


def test_p_base_out_of_range_is_rejected():
    with pytest.raises(InvalidProbability):
        compute_ev(p_base=1.5, lift_prior=Prior(1.0), recoverable_paise=1000, cost_paise=0, action_type="x")
    with pytest.raises(InvalidProbability):
        compute_ev(p_base=-0.1, lift_prior=Prior(1.0), recoverable_paise=1000, cost_paise=0, action_type="x")


def test_negative_recoverable_or_cost_is_rejected():
    with pytest.raises(ValueError):
        compute_ev(p_base=0.5, lift_prior=Prior(1.0), recoverable_paise=-1, cost_paise=0, action_type="x")
    with pytest.raises(ValueError):
        compute_ev(p_base=0.5, lift_prior=Prior(1.0), recoverable_paise=1000, cost_paise=-1, action_type="x")


def test_prior_is_isinstance_checkable_at_runtime_not_just_a_type_hint():
    p = Prior(1.5)
    assert isinstance(p, Prior)
    assert not isinstance(2.0, Prior)


def test_perturb_scales_the_prior_value_only():
    p = Prior(1.0)
    scaled_up = perturb(p, factor=1.5)
    scaled_down = perturb(p, factor=0.5)
    assert scaled_up.value == 1.5
    assert scaled_down.value == 0.5
    assert p.value == 1.0  # original untouched -- Prior isn't mutated in place


def test_decision_flips_when_perturbation_crosses_the_ev_floor():
    # p_base * lift * recoverable - cost = 0.5 * 1.0 * 10_000 - 4_000 = 1_000 (> 0, passes)
    # perturbing lift down by 50%: 0.5 * 0.5 * 10_000 - 4_000 = -1_500 (<= 0, fails) -- flips
    flipped = decision_flips_under_perturbation(
        p_base=0.5, lift_prior=Prior(1.0), recoverable_paise=10_000, cost_paise=4_000,
        action_type="create_mandate", factor=0.5,
    )
    assert flipped is True


def test_decision_does_not_flip_when_comfortably_positive():
    # 0.9 * 3.0 * 100_000 - 1_000 = huge positive; halving lift still huge positive
    flipped = decision_flips_under_perturbation(
        p_base=0.9, lift_prior=Prior(3.0), recoverable_paise=100_000, cost_paise=1_000,
        action_type="create_mandate", factor=0.5,
    )
    assert flipped is False
