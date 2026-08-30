#!/usr/bin/env python3
"""Fit the 'Fitted' parameters DEVDOC_v6 §17.2/§18 name, from the two Kaggle
datasets §18 cites as sources. Run once, commit the output.

    uv run python tools/fit_persona_params.py

Writes data/fitted_params.yaml. Needs pandas/numpy/scikit-learn (dev-only
dependencies -- the running agent never imports them; it would read the
YAML this script produces, not fit anything at runtime).

Both source CSVs are committed in data/ar_seed/ (fetched anonymously via
`kagglehub.dataset_download`, no Kaggle account or API token required --
contrary to this build's earlier assumption that Kaggle access needed
authentication; the classic `kaggle` API/CLI does, `kagglehub` does not for
public datasets). Re-run this script if the source CSVs are ever refreshed.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "ar_seed"
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "data" / "fitted_params.yaml"


def fit_ibm_ar_params() -> dict:
    df = pd.read_csv(DATA_DIR / "ibm_late_payment_histories.csv")
    disputed = df["Disputed"].map({"Yes": True, "No": False})
    amount = df["InvoiceAmount"]

    return {
        "source": "IBM Late Payment Histories (kaggle.com/hhenry/finance-factoring-ibm-late-payment-histories)",
        "fetched_via": "kagglehub.dataset_download, anonymous, no auth required",
        "fetched_at": datetime.now().date().isoformat(),
        "n_invoices": int(len(df)),
        "invoice_amount_distribution_usd": {
            "mean": round(float(amount.mean()), 2),
            "median": round(float(amount.median()), 2),
            "std": round(float(amount.std()), 2),
            "p10": round(float(amount.quantile(0.10)), 2),
            "p90": round(float(amount.quantile(0.90)), 2),
            "note": "USD, from a US dataset -- use as the SHAPE of a distribution "
                    "(spread, skew) for persona generation, not as INR amounts.",
        },
        "dispute_base_rate": round(float(disputed.mean()), 4),
        "days_late_conditional_on_disputed": {
            "disputed": {
                "mean": round(float(df.loc[disputed, "DaysLate"].mean()), 2),
                "median": round(float(df.loc[disputed, "DaysLate"].median()), 2),
                "std": round(float(df.loc[disputed, "DaysLate"].std()), 2),
            },
            "not_disputed": {
                "mean": round(float(df.loc[~disputed, "DaysLate"].mean()), 2),
                "median": round(float(df.loc[~disputed, "DaysLate"].median()), 2),
                "std": round(float(df.loc[~disputed, "DaysLate"].std()), 2),
            },
        },
    }


def fit_p_base_model(horizon_days: int = 30) -> dict:
    df = pd.read_csv(DATA_DIR / "payment_date_prediction.csv")
    closed = df[df["isOpen"] == 0].copy()

    closed["due_date"] = pd.to_datetime(closed["due_in_date"].astype(int).astype(str), format="%Y%m%d")
    closed["clear_date_parsed"] = pd.to_datetime(closed["clear_date"])
    closed["days_late"] = (closed["clear_date_parsed"] - closed["due_date"]).dt.days
    closed["paid_within_horizon"] = (closed["days_late"] <= horizon_days).astype(int)
    closed = closed.dropna(subset=["total_open_amount", "paid_within_horizon"])

    X = np.log1p(closed[["total_open_amount"]].to_numpy())
    y = closed["paid_within_horizon"].to_numpy()

    X_train, X_holdout, y_train, y_holdout = train_test_split(X, y, test_size=0.2, random_state=42)
    scaler = StandardScaler().fit(X_train)
    model = LogisticRegression().fit(scaler.transform(X_train), y_train)

    holdout_pred = model.predict_proba(scaler.transform(X_holdout))[:, 1]
    brier = brier_score_loss(y_holdout, holdout_pred)

    bins = pd.qcut(holdout_pred, q=10, duplicates="drop")
    reliability = (
        pd.DataFrame({"pred": holdout_pred, "actual": y_holdout, "bin": bins})
        .groupby("bin", observed=True)
        .agg(mean_predicted=("pred", "mean"), mean_actual=("actual", "mean"), n=("actual", "size"))
    )

    return {
        "source": "Payment Date Prediction for Invoices (kaggle.com/datasets/pradumn203/payment-date-prediction-for-invoices-dataset)",
        "fetched_via": "kagglehub.dataset_download, anonymous, no auth required",
        "fetched_at": datetime.now().date().isoformat(),
        "n_invoices_total": int(len(df)),
        "n_closed_used_for_fitting": int(len(closed)),
        "n_still_open_excluded": int((df["isOpen"] == 1).sum()),
        "horizon_days": horizon_days,
        "model": "LogisticRegression(feature=log1p(total_open_amount), standardized)",
        "train_size": int(len(X_train)),
        "holdout_size": int(len(X_holdout)),
        "holdout_brier_score": round(float(brier), 4),
        "holdout_base_rate": round(float(y_holdout.mean()), 4),
        "reliability_diagram_deciles": [
            {"mean_predicted": round(float(row.mean_predicted), 4),
             "mean_actual": round(float(row.mean_actual), 4), "n": int(row.n)}
            for row in reliability.itertuples()
        ],
        "coefficients": {
            "intercept": float(model.intercept_[0]),
            "log1p_amount_coef": float(model.coef_[0][0]),
            "scaler_mean": float(scaler.mean_[0]),
            "scaler_scale": float(scaler.scale_[0]),
        },
        "usage_note": (
            "p_base(pay within horizon_days | amount) = sigmoid(intercept + coef * "
            "(log1p(amount) - scaler_mean) / scaler_scale) -- see agent/decide/fitted_p_base.py. "
            "Genuinely fitted and holdout-evaluated, not a declared prior -- but it's a "
            "single-feature model on a US B2B dataset, not an Indian one, and "
            "'total_open_amount' on a closed row is assumed (not independently "
            "verified) to represent the original invoice amount. A stand-in until "
            "Indian-market data exists. See docs/LIMITATIONS.md."
        ),
    }


def main() -> None:
    output = {
        "generated_by": "tools/fit_persona_params.py",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "ibm_ar": fit_ibm_ar_params(),
        "p_base_model": fit_p_base_model(),
    }
    OUTPUT_PATH.write_text(yaml.dump(output, sort_keys=False, default_flow_style=False), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")
    print(f"Dispute base rate: {output['ibm_ar']['dispute_base_rate']:.4f}")
    print(f"p_base holdout Brier score: {output['p_base_model']['holdout_brier_score']:.4f} "
          f"(n={output['p_base_model']['holdout_size']})")


if __name__ == "__main__":
    main()
