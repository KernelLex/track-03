"""Statutory module (MSMED Act) — eligibility, clock, interest. DEVDOC_v6 §14.
Ships rung 4 only (§14.4); rungs 5-6 are documented and stubbed, not implemented.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from pathlib import Path
from typing import Literal

import yaml

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "statutory_params.yaml"

UdyamCategory = Literal["micro", "small", "medium"]
ActivityType = Literal["manufacturing", "services", "trading"]


# ---- §14.1 Eligibility ----


class IneligibilityReason(str, Enum):
    NO_UDYAM = "no_valid_udyam_registration"
    MEDIUM_EXCLUDED = "category_medium_excluded_by_43B_h"
    TRADING_EXCLUDED = "trading_activity_excluded_by_msme_oms"
    STATUS_NOT_INTIMATED = "msme_status_not_intimated_to_buyer"


@dataclass(frozen=True, slots=True)
class EligibilityInput:
    has_valid_udyam_registration: bool
    """Section 2(n) MSMED Act."""
    udyam_category: UdyamCategory
    """Section 43B(h) covers micro and small only. Medium is excluded."""
    invoice_activity_type: ActivityType
    """Invoice-level flag, not supplier-level — a manufacturer's invoice can
    still be a trading transaction depending on what's being sold (§14.1.3)."""
    msme_status_intimated_to_buyer: bool
    """MSME OM No. 2(18)/2007-MSME(pol), 26.08.2008."""


def check_eligibility(inp: EligibilityInput) -> list[IneligibilityReason]:
    """All four conditions are required (§14.1) — returns every reason that
    fails, not just the first, so an approval UI can show the whole picture."""
    reasons: list[IneligibilityReason] = []
    if not inp.has_valid_udyam_registration:
        reasons.append(IneligibilityReason.NO_UDYAM)
    if inp.udyam_category == "medium":
        reasons.append(IneligibilityReason.MEDIUM_EXCLUDED)
    if inp.invoice_activity_type == "trading":
        reasons.append(IneligibilityReason.TRADING_EXCLUDED)
    if not inp.msme_status_intimated_to_buyer:
        reasons.append(IneligibilityReason.STATUS_NOT_INTIMATED)
    return reasons


def is_eligible(inp: EligibilityInput) -> bool:
    return len(check_eligibility(inp)) == 0


# ---- §14.2 Clock ----


def compute_due_date(*, acceptance_date: date, agreement_date: date | None) -> date:
    """agreement_date is SYSTEM-provenance only (an Agreement record, §8) — never
    a MODEL-extracted claim of "there's a written agreement". 45 days is a
    ceiling that exists only with a written agreement; 15 days is the default."""
    if agreement_date is not None:
        return min(agreement_date, acceptance_date + timedelta(days=45))
    return acceptance_date + timedelta(days=15)


# ---- §14.3 Interest ----


class StaleStatutoryParam(Exception):
    """Raised when the configured bank rate is older than its declared shelf
    life. A crash, not a fallback to a stale number (§14.3)."""


@dataclass(frozen=True, slots=True)
class RbiBankRateConfig:
    value: float
    as_of: date
    source: str
    stale_after_days: int = 120

    def assert_fresh(self, today: date) -> None:
        if (today - self.as_of).days > self.stale_after_days:
            raise StaleStatutoryParam(
                f"bank rate config as_of={self.as_of.isoformat()} is more than "
                f"{self.stale_after_days} days old as of {today.isoformat()} — refusing to compute"
            )


@dataclass(frozen=True, slots=True)
class TraderExclusionConfig:
    applied: bool
    basis: str
    position_as_of: date
    contested: bool
    note: str


def load_rbi_bank_rate(path: Path | str = _DEFAULT_CONFIG_PATH) -> RbiBankRateConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)["rbi_bank_rate"]
    return RbiBankRateConfig(
        value=raw["value"],
        as_of=date.fromisoformat(raw["as_of"]),
        source=raw["source"],
        stale_after_days=raw.get("stale_after_days", 120),
    )


def load_trader_exclusion(path: Path | str = _DEFAULT_CONFIG_PATH) -> TraderExclusionConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)["trader_exclusion"]
    return TraderExclusionConfig(
        applied=raw["applied"], basis=raw["basis"],
        position_as_of=date.fromisoformat(raw["position_as_of"]),
        contested=raw["contested"], note=raw["note"],
    )


def compute_statutory_interest_paise(
    *,
    principal_paise: int,
    due_date: date,
    payment_date: date,
    rate_config: RbiBankRateConfig,
    today: date | None = None,
) -> int:
    """Section 16: compound interest, monthly rests, 3x the RBI bank rate, from
    the day after due_date to payment_date (inclusive). Section 23 makes it
    non-deductible for the buyer — a fact for the notice's phrasing, not an
    input to this computation.

    Rounding: nearest paisa at each monthly rest, round-half-up, carried
    forward as principal for the next rest — never a fractional paise
    remainder, matching paise-as-int (§9.1, §14.3).

    Month length is approximated at 30 days for the "monthly rest" boundary,
    a declared simplification (not a calendar-month rest) — see LIMITATIONS.md.
    """
    rate_config.assert_fresh(today or date.today())
    if principal_paise <= 0:
        raise ValueError("principal_paise must be positive")
    if payment_date <= due_date:
        return 0

    monthly_rate = Decimal(str(rate_config.value)) * 3 / 12

    accrual_start = due_date + timedelta(days=1)
    total_days = (payment_date - accrual_start).days + 1
    if total_days <= 0:
        return 0

    full_months, remainder_days = divmod(total_days, 30)

    principal = Decimal(principal_paise)
    for _ in range(full_months):
        interest = principal * monthly_rate
        principal = (principal + interest).quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    if remainder_days > 0:
        daily_rate = monthly_rate / 30
        interest = principal * daily_rate * remainder_days
        principal = (principal + interest).quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    return int(principal) - principal_paise
