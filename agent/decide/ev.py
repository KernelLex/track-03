"""DECIDE stage: expected value, with p_base (fitted) and lift (a declared
prior) kept structurally distinct. DEVDOC_v6 §11.4.

    ev_paise = int(p_base * lift_prior * recoverable_paise) - cost_paise(action)
    #          ^^^^^^ fitted, calibrated   ^^^^^^^^^^ a declared prior

Neither `p_base` nor `lift_prior` is *fitted* by this module — `p_base`
needs the Kaggle IBM Late Payment Histories / Payment Date Prediction
datasets fitted and calibrated (§17.5), which this build doesn't have (see
docs/LIMITATIONS.md). What's built here is the honest arithmetic and the
type-level distinction DEVDOC_v6 §11.4 itself calls for: `lift_prior` is
wrapped in `Prior`, not a bare `float`, specifically so a code reviewer
can't mistake a declared prior for a fitted value in a diff — and because
it's a real class rather than a `NewType` alias, `isinstance(x, Prior)` is
checkable at runtime, not just a type-checker fiction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


class Prior(Generic[T]):
    """A value that is a declared prior, not a fitted estimate."""

    __slots__ = ("value",)

    def __init__(self, value: T):
        self.value = value

    def __repr__(self) -> str:
        return f"Prior({self.value!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Prior) and self.value == other.value

    def __hash__(self) -> int:
        return hash(("Prior", self.value))


class InvalidProbability(Exception):
    """p_base must be a probability. Never silently clamped into range."""


@dataclass(frozen=True, slots=True)
class Decision:
    action_type: str
    p_base: float
    """Fitted, calibrated — probability this invoice gets paid by T absent
    intervention. Not wrapped in Prior: this is the fitted half."""
    lift_prior: "Prior[float]"
    recoverable_paise: int
    cost_paise: int
    ev_paise: int


def compute_ev(
    *, p_base: float, lift_prior: "Prior[float]", recoverable_paise: int, cost_paise: int, action_type: str
) -> Decision:
    if not (0.0 <= p_base <= 1.0):
        raise InvalidProbability(f"p_base must be in [0, 1], got {p_base}")
    if not isinstance(lift_prior, Prior):
        raise TypeError("lift_prior must be a Prior[float], not a bare float — see DEVDOC_v6 §11.4")
    if recoverable_paise < 0:
        raise ValueError("recoverable_paise cannot be negative")
    if cost_paise < 0:
        raise ValueError("cost_paise cannot be negative")

    ev_paise = int(p_base * lift_prior.value * recoverable_paise) - cost_paise
    return Decision(
        action_type=action_type, p_base=p_base, lift_prior=lift_prior,
        recoverable_paise=recoverable_paise, cost_paise=cost_paise, ev_paise=ev_paise,
    )


def perturb(lift_prior: "Prior[float]", *, factor: float) -> "Prior[float]":
    """§17.5's decision-flip-rate methodology: perturb lift by e.g. +/-50%
    (factor=1.5 or 0.5) and see whether the decision (here: EV's sign) flips.
    This is the one piece of §17.5 that's meaningful without a full eval run —
    see `decision_flips_under_perturbation` below for the actual flip check."""
    return Prior(lift_prior.value * factor)


def decision_flips_under_perturbation(
    *, p_base: float, lift_prior: "Prior[float]", recoverable_paise: int, cost_paise: int,
    action_type: str, factor: float,
) -> bool:
    """True if perturbing lift_prior by `factor` changes whether EV_FLOOR would
    pass (ev_paise > 0). A low flip rate across many (action, family) cells
    means the prior isn't load-bearing; a high one means it is — §17.5's own
    framing, which this function answers for one cell at a time."""
    original = compute_ev(
        p_base=p_base, lift_prior=lift_prior, recoverable_paise=recoverable_paise,
        cost_paise=cost_paise, action_type=action_type,
    )
    perturbed = compute_ev(
        p_base=p_base, lift_prior=perturb(lift_prior, factor=factor), recoverable_paise=recoverable_paise,
        cost_paise=cost_paise, action_type=action_type,
    )
    return (original.ev_paise > 0) != (perturbed.ev_paise > 0)
