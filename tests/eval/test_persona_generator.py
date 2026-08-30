"""eval/personas/generator.py -- reproducibility and the fitted properties
a large population should approximately reproduce."""

from __future__ import annotations

from eval.personas.generator import (
    ASSUMED_CONTACT_TOLERANCE_RANGE,
    DISPUTE_BASE_RATE,
    Blocker,
    generate_population,
)


class TestGeneratePopulation:
    def test_rejects_non_positive_n(self):
        import pytest
        with pytest.raises(ValueError):
            generate_population(0, seed=1)

    def test_same_seed_is_fully_reproducible(self):
        a = generate_population(200, seed=7)
        b = generate_population(200, seed=7)
        assert a == b

    def test_different_seeds_produce_different_populations(self):
        a = generate_population(200, seed=1)
        b = generate_population(200, seed=2)
        assert a != b

    def test_returns_exactly_n_personas_with_unique_ids(self):
        personas = generate_population(150, seed=3)
        assert len(personas) == 150
        assert len({p.id for p in personas}) == 150

    def test_amounts_are_positive(self):
        personas = generate_population(500, seed=4)
        assert all(p.amount_paise > 0 for p in personas)

    def test_p_base_is_a_probability(self):
        personas = generate_population(500, seed=5)
        assert all(0.0 <= p.p_base <= 1.0 for p in personas)

    def test_contact_tolerance_within_the_declared_range(self):
        lo, hi = ASSUMED_CONTACT_TOLERANCE_RANGE.value
        personas = generate_population(500, seed=6)
        assert all(lo <= p.contact_tolerance <= hi for p in personas)

    def test_dispute_rate_approximately_matches_the_fitted_base_rate(self):
        """Not exact -- it's a random draw -- but at n=5000 the sample rate
        should land close to the fitted 0.2275 from the IBM AR dataset."""
        personas = generate_population(5000, seed=8)
        disputed_fraction = sum(p.true_blocker is Blocker.DISPUTE for p in personas) / len(personas)
        assert abs(disputed_fraction - DISPUTE_BASE_RATE) < 0.03

    def test_every_disputed_persona_has_blocker_dispute_and_vice_versa_is_not_assumed(self):
        """DISPUTE is reserved for the fitted dispute draw -- this just checks
        the enum value used is the real Blocker.DISPUTE member, not a stand-in."""
        personas = generate_population(300, seed=9)
        assert any(p.true_blocker is Blocker.DISPUTE for p in personas)
        assert any(p.true_blocker is Blocker.NONE for p in personas)
