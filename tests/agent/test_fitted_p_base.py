"""p_base loaded from real fitted coefficients (data/fitted_params.yaml, produced
by tools/fit_persona_params.py against the actual Kaggle dataset). DEVDOC_v6 §11.4, §17.5."""

from __future__ import annotations

import math

import pytest

from agent.decide.fitted_p_base import FittedPBaseModel, load_fitted_p_base


@pytest.fixture(scope="module")
def model():
    return load_fitted_p_base()


def test_loads_real_committed_coefficients(model):
    assert model.horizon_days == 30
    assert 0.0 <= model.holdout_brier_score <= 0.25  # 0.25 is the always-predict-0.5 ceiling
    assert 0.0 <= model.holdout_base_rate <= 1.0


def test_predict_returns_a_valid_probability(model):
    p = model.predict(50_000_00)  # Rs 50,000
    assert 0.0 <= p <= 1.0


def test_predict_is_monotonic_in_the_direction_the_fitted_coefficient_implies(model):
    small = model.predict(1_000_00)
    large = model.predict(500_000_00)
    if model.log1p_amount_coef > 0:
        assert large > small
    else:
        assert large < small


def test_predict_rejects_non_positive_amounts(model):
    with pytest.raises(ValueError):
        model.predict(0)
    with pytest.raises(ValueError):
        model.predict(-100)


def test_predict_matches_manual_sigmoid_computation(model):
    amount_paise = 25_000_00
    amount = amount_paise / 100
    standardized = (math.log1p(amount) - model.scaler_mean) / model.scaler_scale
    logit = model.intercept + model.log1p_amount_coef * standardized
    expected = 1.0 / (1.0 + math.exp(-logit))
    assert model.predict(amount_paise) == pytest.approx(expected)


def test_predictions_stay_within_the_narrow_band_the_reliability_diagram_shows():
    """Honest finding, not a bug: the fitted model's predictions cluster in a
    narrow band (~0.96-0.985 per data/fitted_params.yaml's reliability_diagram_deciles)
    because amount alone is a weak discriminator against a 97.9% base rate in this
    dataset. This test pins that finding so a future re-fit doesn't silently drift
    without anyone noticing the model became either trivial or wildly miscalibrated."""
    model = load_fitted_p_base()
    predictions = [model.predict(amount_paise) for amount_paise in (100_00, 10_000_00, 1_000_000_00)]
    assert all(0.90 <= p <= 1.0 for p in predictions)
