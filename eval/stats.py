"""Uncertainty for the rates this project reports.

Every arm rate in `docs/RESULTS.md` was a bare point estimate: 98.4%, 96.2%,
77.6%, from 500 personas, with nothing saying how much a different 500 would
move them. In a project whose entire argument is that its claims are
checkable, an unqualified proportion is the one kind of number a reader
cannot check.

The gap that actually mattered was not the headline. Arm C vs Arm A is
enormous and obviously real. **Arm C vs Arm B2 is 2.2 points**, and whether
that survives sampling noise is exactly the question a careful reader asks
first -- and the one the report could not answer.

Two functions, both pure, both tested against published reference values
rather than against themselves.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Z_95 = 1.959963984540054
"""The two-sided 95% normal quantile. Spelled out rather than 1.96 because
the tests compare against published interval tables computed at this
precision, and 1.96 visibly shifts the fourth decimal."""


@dataclass(frozen=True, slots=True)
class Interval:
    point: float
    low: float
    high: float

    def as_pct(self, places: int = 1) -> str:
        return (f"{self.point:.{places}%} "
                f"[{self.low:.{places}%}, {self.high:.{places}%}]")


def wilson_interval(successes: int, n: int, z: float = Z_95) -> Interval:
    """Wilson score interval for a proportion.

    **Why Wilson rather than the textbook `p +/- z*sqrt(p(1-p)/n)`.** That
    normal approximation is wrong in exactly the region this project reports
    in. At 98.4% of 500 it gives an upper bound of 99.5%, and a little
    higher it produces bounds *above 100%* -- a confidence interval that
    includes impossible values. It also collapses to zero width at p=0 or
    p=1, claiming perfect certainty from a sample that has merely not yet
    seen the other outcome.

    Wilson does neither: it is bounded in [0, 1] by construction and stays
    sensible at the extremes, which is where recovery rates live.

    n=0 returns the whole interval rather than raising -- an arm with no
    observations is maximally uncertain, not an error.
    """
    if successes < 0 or n < 0 or successes > n:
        raise ValueError(f"successes={successes} must be within 0..n for n={n}")
    if n == 0:
        return Interval(point=0.0, low=0.0, high=1.0)

    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return Interval(point=p, low=max(0.0, centre - half), high=min(1.0, centre + half))


@dataclass(frozen=True, slots=True)
class ProportionTest:
    p_a: float
    p_b: float
    difference_pp: float
    z: float
    p_value: float

    @property
    def significant_at_05(self) -> bool:
        return self.p_value < 0.05

    def describe(self) -> str:
        verdict = "significant" if self.significant_at_05 else "not significant"
        return (f"{self.difference_pp:+.1f} pp, z = {self.z:.2f}, "
                f"p = {self.p_value:.4g} ({verdict} at 0.05)")


def two_proportion_test(successes_a: int, n_a: int, successes_b: int, n_b: int) -> ProportionTest:
    """Two-sided z-test for a difference between two independent proportions.

    Pooled variance under the null that both arms share one underlying rate,
    which is the hypothesis actually being tested -- "these two arms are the
    same" -- rather than the unpooled form that assumes they differ.

    Reports the p-value whatever it is. A test run only to confirm a
    difference you already believe in is decoration; the value here is that
    it can come back saying the gap is not distinguishable from noise, and
    that answer gets published too.
    """
    if n_a <= 0 or n_b <= 0:
        raise ValueError("both arms need at least one observation")

    p_a, p_b = successes_a / n_a, successes_b / n_b
    pooled = (successes_a + successes_b) / (n_a + n_b)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n_a + 1 / n_b))
    if se == 0:
        # Both arms identical and degenerate (all-success or all-failure).
        # No evidence of a difference, and no division by zero either.
        return ProportionTest(p_a=p_a, p_b=p_b, difference_pp=0.0, z=0.0, p_value=1.0)

    z = (p_b - p_a) / se
    p_value = math.erfc(abs(z) / math.sqrt(2))
    return ProportionTest(
        p_a=p_a, p_b=p_b, difference_pp=(p_b - p_a) * 100, z=z, p_value=p_value,
    )
