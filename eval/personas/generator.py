"""Synthetic debtor personas for the Monte Carlo simulation harness.
DEVDOC_v6 §17.1-§17.2.

Every value below is labelled FITTED or an ASSUMED (declared prior), and the
two are never blended silently. FITTED values trace to
`data/fitted_params.yaml` (real Kaggle data, `tools/fit_persona_params.py`).
ASSUMED values exist because **no dataset gives counterfactual intervention
response for Indian B2B AR** (DEVDOC_v6 §17.1, `agent/decide/ev.py`'s own
docstring) — how a population splits across blocker types, and how many
unwanted touches a debtor tolerates before opting out, are not in the
Kaggle data or anywhere else this project has access to. ASSUMED constants
are wrapped in `agent.decide.ev.Prior` for the same reason `lift_prior` is:
so a reader (or a diff) can't mistake a declared assumption for a fitted
estimate.

This module produces *inputs* to the simulation (`eval/simulate.py`) — it
has no opinion on which arm helps which persona. That separation matters:
the population must be the same regardless of which arm evaluates it, or a
comparison across arms isn't measuring the arms.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum

from agent.decide.ev import Prior
from agent.decide.fitted_p_base import FittedPBaseModel, load_fitted_p_base


class Blocker(str, Enum):
    """The persona's TRUE underlying situation — deliberately a separate,
    smaller enum from agent.diagnose.extract.Family/DiagnosisClass rather
    than reusing them: this simulation only distinguishes four coarse
    situations, and pretending that maps precisely onto the real 29-class
    taxonomy would overstate this model's precision. `eval/simulate.py`
    maps a *diagnosed* Blocker onto a real Family only where it needs to
    call real code (ACTIONS_UNLOCKED) that's keyed on Family."""

    NONE = "none"
    """No real blocker — resolves without any specific intervention, on
    its own timeline. Driven by the FITTED p_base model."""
    INSTRUMENT = "instrument"
    """Family-A-shaped: retrying the same failed rail doesn't help."""
    ADMINISTRATIVE = "administrative"
    """Family-B-shaped: a reissued artifact or reconciliation request helps."""
    DISPUTE = "dispute"
    """Family-D-shaped: only human escalation actually resolves this."""


# --- FITTED (data/fitted_params.yaml, tools/fit_persona_params.py) ---

DISPUTE_BASE_RATE = 0.2275
"""IBM AR set, 2,466 invoices — the unconditional share with a genuine
dispute. The dataset doesn't condition this on amount, so neither does this
sampler."""

_AMOUNT_USD_MEAN = 59.90
_AMOUNT_USD_STD = 20.44
"""Payment Date Prediction dataset. Used only for the *shape* (coefficient
of variation) of the amount distribution — the dataset's own documentation
(data/fitted_params.yaml) is explicit these are USD figures from a US
dataset and must not be read as INR amounts."""

# --- ASSUMED (declared priors — §17.2's "no credible source exists" rows) ---

ASSUMED_MEDIAN_INVOICE_PAISE = Prior(50_000_00)
"""Rs 50,000 as the population's median invoice size — a chosen anchor for
an Indian B2B AR context, not fitted from any dataset this project has."""

ASSUMED_BLOCKER_SPLIT_AMONG_UNRESOLVED = Prior({
    Blocker.INSTRUMENT: 0.30, Blocker.ADMINISTRATIVE: 0.35, Blocker.DISPUTE: 0.35,
})
"""Given a persona is disputed OR won't resolve on its own within the
window, how the remainder splits across the three real blocker types.
Disputed personas are always Blocker.DISPUTE (that part is fitted-adjacent —
it follows directly from the fitted dispute draw); this split only applies
to the non-disputed, won't-pay-on-its-own remainder, and is an even-ish
prior over the other three, not a data-driven proportion."""

ASSUMED_CONTACT_TOLERANCE_RANGE = Prior((3, 8))
"""Touches on a single channel, without resolution, before a persona opts
out of that channel — drawn uniformly from this range per persona. DEVDOC_v6
§17.1 names exactly this kind of number as unmeasurable without real
intervention-response data; kept in the same neighborhood as this project's
own CHANNEL_EXHAUSTION design assumption rather than tuned for effect."""


@dataclass(frozen=True, slots=True)
class Persona:
    id: str
    amount_paise: int
    true_blocker: Blocker
    p_base: float
    """The fitted model's P(resolves within the window | amount) — reused
    directly as compute_ev()'s p_base input for Arm C, so the same fitted
    number drives both persona generation and the decision it's compared
    against, rather than two independently-invented numbers."""
    contact_tolerance: int


def _sample_amount_paise(rng: random.Random, median_paise: int) -> int:
    cv = _AMOUNT_USD_STD / _AMOUNT_USD_MEAN
    draw = max(0.05, rng.gauss(1.0, cv))
    return max(100_00, round(draw * median_paise / 100) * 100)


def generate_population(
    n: int,
    *,
    seed: int,
    p_base_model: FittedPBaseModel | None = None,
) -> list[Persona]:
    """Deterministic given `seed` — the same call always returns the same
    population, so an arm comparison is reproducible and re-runnable by
    anyone, not just something that happened once in this session."""
    if n <= 0:
        raise ValueError("n must be positive")

    rng = random.Random(seed)
    p_base_model = p_base_model or load_fitted_p_base()
    median_paise = ASSUMED_MEDIAN_INVOICE_PAISE.value
    blocker_split = ASSUMED_BLOCKER_SPLIT_AMONG_UNRESOLVED.value
    tolerance_lo, tolerance_hi = ASSUMED_CONTACT_TOLERANCE_RANGE.value

    personas: list[Persona] = []
    for i in range(n):
        amount_paise = _sample_amount_paise(rng, median_paise)
        p_base = p_base_model.predict(amount_paise)
        is_disputed = rng.random() < DISPUTE_BASE_RATE
        resolves_on_its_own = (not is_disputed) and rng.random() < p_base

        if is_disputed:
            blocker = Blocker.DISPUTE
        elif resolves_on_its_own:
            blocker = Blocker.NONE
        else:
            blocker = rng.choices(
                population=list(blocker_split.keys()), weights=list(blocker_split.values()), k=1,
            )[0]

        personas.append(Persona(
            id=f"persona_{i:05d}",
            amount_paise=amount_paise,
            true_blocker=blocker,
            p_base=p_base,
            contact_tolerance=rng.randint(tolerance_lo, tolerance_hi),
        ))
    return personas
