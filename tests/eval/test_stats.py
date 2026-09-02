"""Uncertainty arithmetic, checked against published values rather than
against itself.

A statistics helper that only agrees with its own output proves nothing --
so the interval cases below come from the standard Wilson reference tables
(Newcombe 1998, "Two-sided confidence intervals for the single proportion"),
and the z-test cases are hand-computable.
"""

from __future__ import annotations

import pytest

from eval.stats import Interval, two_proportion_test, wilson_interval


class TestWilsonAgainstPublishedValues:
    """Newcombe's own worked examples, at 95%."""

    @pytest.mark.parametrize("successes,n,low,high", [
        (81, 263, 0.2553, 0.3662),
        (15, 148, 0.0624, 0.1605),
        (0, 20, 0.0000, 0.1611),
        # 0.1718, not the 0.1704 that Newcombe also prints for this row --
        # that column is the *continuity-corrected* Wilson interval, a
        # different estimator. Confirmed against the defining quadratic
        # below rather than by adjusting the code to a remembered number.
        (1, 29, 0.0061, 0.1718),
    ])
    def test_it_matches_the_reference_table(self, successes, n, low, high):
        interval = wilson_interval(successes, n)
        assert interval.low == pytest.approx(low, abs=5e-4)
        assert interval.high == pytest.approx(high, abs=5e-4)


class TestAgainstAnIndependentDerivation:
    """Stronger than any reference table, because it does not depend on my
    having remembered one correctly -- which, on the 1/29 row above, I had
    not.

    The Wilson interval is *defined* as the set of p satisfying
    `|p_hat - p| / sqrt(p(1-p)/n) <= z`. Solving that quadratic directly is
    a different route to the same two numbers, so agreement is real
    corroboration rather than the implementation agreeing with itself.
    """

    @pytest.mark.parametrize("successes,n", [
        (81, 263), (15, 148), (0, 20), (1, 29),
        (388, 500), (481, 500), (492, 500),   # this project's own arm rates
    ])
    def test_it_solves_the_interval_s_defining_equation(self, successes, n):
        import math

        from eval.stats import Z_95

        z, p_hat = Z_95, successes / n
        # (n + z^2)p^2 - (2*n*p_hat + z^2)p + n*p_hat^2 = 0
        a = n + z * z
        b = -(2 * n * p_hat + z * z)
        c = n * p_hat * p_hat
        root = math.sqrt(b * b - 4 * a * c)
        expected_low, expected_high = (-b - root) / (2 * a), (-b + root) / (2 * a)

        interval = wilson_interval(successes, n)
        assert interval.low == pytest.approx(expected_low, abs=1e-12)
        assert interval.high == pytest.approx(expected_high, abs=1e-12)


class TestTheReasonItIsWilson:
    """The normal approximation is wrong in exactly the region this project
    reports in, which is the whole argument for choosing this one."""

    def test_it_never_exceeds_one(self):
        """`p +/- z*sqrt(p(1-p)/n)` produces upper bounds above 100% near
        the ceiling -- a confidence interval containing impossible values."""
        for n in (20, 50, 200, 500):
            assert wilson_interval(n, n).high <= 1.0

    def test_it_never_goes_below_zero(self):
        for n in (20, 50, 200, 500):
            assert wilson_interval(0, n).low >= 0.0

    def test_it_does_not_claim_certainty_at_the_extremes(self):
        """The normal approximation collapses to zero width at p=0 and p=1,
        claiming perfect certainty from a sample that has simply not seen
        the other outcome yet. 20 successes out of 20 is not proof of 100%."""
        perfect = wilson_interval(20, 20)
        assert perfect.point == 1.0
        assert perfect.low < 0.85, "an all-success sample must still admit doubt"

        none = wilson_interval(0, 20)
        assert none.point == 0.0
        assert none.high > 0.10

    def test_more_data_narrows_it(self):
        wide = wilson_interval(8, 10)
        narrow = wilson_interval(800, 1000)
        assert (narrow.high - narrow.low) < (wide.high - wide.low)

    def test_no_observations_is_maximum_uncertainty_not_an_error(self):
        assert wilson_interval(0, 0) == Interval(point=0.0, low=0.0, high=1.0)

    def test_impossible_counts_are_refused(self):
        with pytest.raises(ValueError):
            wilson_interval(11, 10)


class TestTheDifferenceBetweenArms:
    def test_a_large_gap_is_overwhelmingly_significant(self):
        """Arm A 77.6% vs Arm C 98.4% at n=500 -- the headline comparison."""
        result = two_proportion_test(388, 500, 492, 500)
        assert result.p_value < 1e-20
        assert result.significant_at_05

    def test_the_gap_that_actually_needed_testing(self):
        """Arm B2 96.2% vs Arm C 98.4%. Only 2.2 points, and the one a
        careful reader questions first -- B2 is the *policy-aware* chaser,
        so this is where the gated pipeline has to earn its claim."""
        result = two_proportion_test(481, 500, 492, 500)
        assert result.difference_pp == pytest.approx(2.2, abs=0.05)
        assert 0.01 < result.p_value < 0.06, (
            "this comparison is significant but not overwhelming -- reporting "
            "it as either extreme would misrepresent it")

    def test_identical_arms_are_not_a_difference(self):
        result = two_proportion_test(400, 500, 400, 500)
        assert result.difference_pp == pytest.approx(0.0)
        assert result.p_value == pytest.approx(1.0, abs=1e-9)

    def test_it_reports_non_significance_rather_than_hiding_it(self):
        """The value of the test is that it can come back negative. 50% vs
        54% at n=100 is noise, and must be reported as noise."""
        result = two_proportion_test(50, 100, 54, 100)
        assert not result.significant_at_05
        assert "not significant" in result.describe()

    def test_it_is_symmetric_in_magnitude(self):
        forward = two_proportion_test(388, 500, 492, 500)
        backward = two_proportion_test(492, 500, 388, 500)
        assert forward.p_value == pytest.approx(backward.p_value)
        assert forward.z == pytest.approx(-backward.z)

    def test_an_empty_arm_is_refused(self):
        with pytest.raises(ValueError):
            two_proportion_test(0, 0, 5, 10)
