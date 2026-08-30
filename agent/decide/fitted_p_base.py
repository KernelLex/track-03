"""p_base, actually fitted. DEVDOC_v6 §11.4, §17.5.

The coefficients here come from `data/fitted_params.yaml`, produced by
`tools/fit_persona_params.py` fitting a logistic regression against the
Payment Date Prediction for Invoices dataset (§18) — holdout Brier score
and a reliability diagram are recorded there, not just a point estimate.
This module reimplements the fitted sigmoid directly (`math.exp`, no
pandas/scikit-learn) so the running agent never needs the fitting
dependencies — only `tools/fit_persona_params.py` does.

**Read the honest finding, not just the low Brier score.** The dataset's
holdout base rate is 97.9% (almost everything pays within 30 days
regardless of amount) — a Brier score of 0.02 on that target is close to
what a same-as-base-rate model would get. The reliability diagram (in
`data/fitted_params.yaml`) shows the model IS well-calibrated across
deciles, but its predictions only range ~0.96-0.985: amount alone is a
weak discriminator in this dataset. That's a real result about this
feature and this (US, not Indian) dataset, not a claim that `p_base` is
strongly predictive here — see docs/LIMITATIONS.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import yaml

_DEFAULT_PARAMS_PATH = Path(__file__).resolve().parents[2] / "data" / "fitted_params.yaml"


@dataclass(frozen=True, slots=True)
class FittedPBaseModel:
    intercept: float
    log1p_amount_coef: float
    scaler_mean: float
    scaler_scale: float
    horizon_days: int
    holdout_brier_score: float
    holdout_base_rate: float

    def predict(self, amount_paise: int) -> float:
        """P(paid within horizon_days of due date | amount). amount_paise is
        converted to the same unit the model was fit on (the source dataset is
        USD, so this reuses the raw magnitude — see the module docstring's
        caveat about this being a stand-in, not an INR-fitted model)."""
        if amount_paise <= 0:
            raise ValueError("amount_paise must be positive")
        amount = amount_paise / 100  # paise -> the dataset's raw currency-unit magnitude
        log1p_amount = math.log1p(amount)
        standardized = (log1p_amount - self.scaler_mean) / self.scaler_scale
        logit = self.intercept + self.log1p_amount_coef * standardized
        return 1.0 / (1.0 + math.exp(-logit))


def load_fitted_p_base(path: Path | str = _DEFAULT_PARAMS_PATH) -> FittedPBaseModel:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)["p_base_model"]
    coeffs = raw["coefficients"]
    return FittedPBaseModel(
        intercept=coeffs["intercept"],
        log1p_amount_coef=coeffs["log1p_amount_coef"],
        scaler_mean=coeffs["scaler_mean"],
        scaler_scale=coeffs["scaler_scale"],
        horizon_days=raw["horizon_days"],
        holdout_brier_score=raw["holdout_brier_score"],
        holdout_base_rate=raw["holdout_base_rate"],
    )
