"""What a debtor's track record earns them.

`promise_credibility` has been referenced by `PROMISE_COOLDOWN` since the
bounds gate was written -- the rule scales its grace period by it in both
implementations -- and nothing ever computed it. Every context used the
`1.0` default, so a debtor who had broken four promises got exactly the
same quiet time as one who had never broken any.

The properties worth holding are mostly about what the score is *not*
allowed to do: invent a penalty, punish someone for having no history, or
count a promise as broken before its date arrives.
"""

from __future__ import annotations

from datetime import date, timedelta

from agent.debtor.registry import Debtor, DebtorRegistry
from agent.debtor.score import (
    CREDIBILITY_WINDOW,
    NO_HISTORY_CREDIBILITY,
    PromiseOutcome,
    promise_credibility,
    terms_for,
)
from agent.debtor.seed import seed_registry

TODAY = date(2026, 9, 1)


def _outcome(outcome, *, days_ago=30, amount=10_000_00):
    return PromiseOutcome(invoice_id="INV-1", promised_amount_paise=amount,
                          promised_date=TODAY - timedelta(days=days_ago), outcome=outcome)


class TestTheScoreItself:
    def test_no_history_gets_the_benefit_of_the_doubt(self):
        """Starting everyone at zero would apply the strictest terms to the
        debtors this system knows least about -- unfair, and self-defeating,
        since it refuses the instalment plan most likely to get them to pay."""
        assert promise_credibility([]) == NO_HISTORY_CREDIBILITY
        assert terms_for([]).band == "trusted"

    def test_all_kept_is_full_credibility(self):
        assert promise_credibility([_outcome("kept")] * 4) == 1.0

    def test_all_broken_is_zero(self):
        assert promise_credibility([_outcome("broken")] * 3) == 0.0

    def test_a_pending_promise_is_not_counted_as_broken(self):
        """A promise whose date hasn't arrived is not evidence of anything.
        Counting it against them would penalise a debtor at the moment they
        made a commitment -- the opposite of the intended incentive."""
        outcomes = [_outcome("kept"), _outcome("pending"), _outcome("pending")]
        assert promise_credibility(outcomes) == 1.0
        assert terms_for(outcomes).resolved_promises == 1

    def test_only_the_trailing_window_counts(self):
        """Old failures age out -- a debtor who has since paid five in a row
        should not be held to a year-old miss."""
        outcomes = [_outcome("broken")] * 3 + [_outcome("kept")] * CREDIBILITY_WINDOW
        assert promise_credibility(outcomes) == 1.0


class TestTheScoreDecidesRealTerms:
    def test_a_perfect_record_earns_the_longest_grace_and_a_split(self):
        terms = terms_for([_outcome("kept")] * 4)
        assert terms.band == "trusted"
        assert terms.grace_days == 10
        assert terms.max_instalments == 4
        assert terms.offers_instalment_plan is True

    def test_a_poor_record_earns_one_day_and_no_split(self):
        terms = terms_for([_outcome("broken")] * 4 + [_outcome("kept")])
        assert terms.band == "strict"
        assert terms.grace_days == 1
        assert terms.offers_instalment_plan is False
        assert terms.early_discount_rate == 0.0

    def test_grace_is_monotonic_in_the_score(self):
        """Not a cutoff -- a sliding scale, the same shape PROMISE_COOLDOWN's
        own comment describes."""
        grace = [terms_for([_outcome("kept")] * k + [_outcome("broken")] * (4 - k)).grace_days
                 for k in range(5)]
        assert grace == sorted(grace)

    def test_even_the_worst_record_keeps_a_route_to_pay(self):
        """A score is not a cutoff. A debtor with nothing kept still gets a
        grace day and a single-instalment plan, because the point is to be
        paid, not to punish."""
        terms = terms_for([_outcome("broken")] * 5)
        assert terms.grace_days >= 1
        assert terms.max_instalments >= 1

    def test_the_rationale_says_what_the_score_was_built_from(self):
        """A debtor asking "why am I being offered this" deserves an answer
        that isn't 'the model decided'."""
        rationale = terms_for([_outcome("kept"), _outcome("broken")]).rationale
        assert "1 of the last 2" in rationale
        assert "rail-confirmed captures" in rationale


class TestWhatTheScoreMayNotDo:
    def test_it_never_raises_what_is_owed(self):
        """Statutory interest is set by law (agent/statutory/msmed.py). A
        score may decide whether to *press* a claim; it can never invent a
        late fee, which is the penalty this project refuses to produce."""
        worst = terms_for([_outcome("broken")] * 5)
        best = terms_for([_outcome("kept")] * 5)
        for terms in (worst, best):
            assert not hasattr(terms, "late_fee_rate")
            assert not hasattr(terms, "penalty_paise")
        # The only lever is whether the statutory claim is pressed at all.
        assert worst.press_statutory_interest is True
        assert best.press_statutory_interest is False

    def test_the_discount_is_never_better_than_the_published_rate(self):
        """Bands may reduce a voluntary discount; none may exceed the
        published one, so the score can't be used to invent an inducement."""
        from agent.mandate.early_payment import DEFAULT_DISCOUNT_RATE

        for kept in range(6):
            terms = terms_for([_outcome("kept")] * kept + [_outcome("broken")] * (5 - kept))
            assert 0.0 <= terms.early_discount_rate <= DEFAULT_DISCOUNT_RATE


class TestTheRegistry:
    def test_a_capture_keeps_the_oldest_open_promise(self, tmp_path):
        r = DebtorRegistry(str(tmp_path / "d.db"))
        r.upsert(Debtor(id="d1", display_name="D", channel="telegram", channel_ref="c1",
                        invoice_id="INV-1", invoice_amount_paise=10_000_00, is_seeded=False))
        r.record_promise("d1", invoice_id="INV-1", amount_paise=5_000_00, promised_date="2026-09-05")
        r.record_promise("d1", invoice_id="INV-1", amount_paise=5_000_00, promised_date="2026-09-19")

        assert r.settle_promise("d1", payment_id="pay_1", invoice_id="INV-1") is True
        outcomes = r.outcomes_for("d1")
        assert [o.outcome for o in outcomes] == ["kept", "pending"]
        assert outcomes[0].payment_id == "pay_1"
        r.close()

    def test_the_same_capture_cannot_improve_a_score_twice(self, tmp_path):
        """Same discipline as RecoveryLedger's UNIQUE(payment_id): a
        redelivered webhook must not be able to count again."""
        r = DebtorRegistry(str(tmp_path / "d.db"))
        r.upsert(Debtor(id="d1", display_name="D", channel="telegram", channel_ref="c1",
                        invoice_id="INV-1", invoice_amount_paise=10_000_00, is_seeded=False))
        r.record_promise("d1", invoice_id="INV-1", amount_paise=5_000_00, promised_date="2026-09-05")
        r.record_promise("d1", invoice_id="INV-1", amount_paise=5_000_00, promised_date="2026-09-19")
        r.settle_promise("d1", payment_id="pay_1", invoice_id="INV-1")
        r.record_promise("d1", invoice_id="INV-1", amount_paise=1_00_00,
                         promised_date="2026-10-01", outcome="kept", payment_id="pay_1")

        kept = [o for o in r.outcomes_for("d1") if o.outcome == "kept"]
        assert len(kept) == 1
        r.close()

    def test_a_date_that_passed_without_payment_breaks_the_promise(self, tmp_path):
        """Time passing resolves this, not a judgement about the debtor --
        which is what makes the resulting score defensible to them."""
        r = DebtorRegistry(str(tmp_path / "d.db"))
        r.upsert(Debtor(id="d1", display_name="D", channel="telegram", channel_ref="c1",
                        invoice_id="INV-1", invoice_amount_paise=10_000_00, is_seeded=False))
        r.record_promise("d1", invoice_id="INV-1", amount_paise=5_000_00, promised_date="2026-08-01")
        r.record_promise("d1", invoice_id="INV-1", amount_paise=5_000_00, promised_date="2099-01-01")

        assert r.expire_overdue_promises("d1", today=TODAY) == 1
        assert [o.outcome for o in r.outcomes_for("d1")] == ["broken", "pending"]
        r.close()


class TestSeeding:
    def test_it_spans_the_bands_so_the_scoring_is_demonstrable(self, tmp_path):
        r = DebtorRegistry(str(tmp_path / "d.db"))
        seed_registry(r, today=TODAY)
        bands = {d.id: r.terms(d.id).band for d in r.all_debtors()}
        assert set(bands.values()) >= {"trusted", "standard", "strict"}
        r.close()

    def test_seeded_debtors_are_marked_as_fixtures(self, tmp_path):
        """A declared history must never read as evidence of real
        behaviour -- the same overclaim docs/RESULTS.md refuses to make."""
        r = DebtorRegistry(str(tmp_path / "d.db"))
        seed_registry(r, today=TODAY)
        assert all(d.is_seeded for d in r.all_debtors() if d.id != "debtor_live")
        r.close()

    def test_seeding_twice_does_not_inflate_a_history(self, tmp_path):
        """It runs on every boot, and Render restarts often."""
        r = DebtorRegistry(str(tmp_path / "d.db"))
        seed_registry(r, today=TODAY)
        before = len(r.outcomes_for("debtor_orbit"))
        seed_registry(r, today=TODAY)
        assert len(r.outcomes_for("debtor_orbit")) == before
        r.close()

    def test_the_real_debtor_starts_with_no_declared_history(self, tmp_path, monkeypatch):
        """Pre-loading the live user would contaminate the one genuinely
        real row in the table."""
        monkeypatch.setenv("DEMO_CONTACT_TELEGRAM_CHAT_ID", "12345")
        r = DebtorRegistry(str(tmp_path / "d.db"))
        seed_registry(r, today=TODAY)

        live = r.debtor("debtor_live")
        assert live is not None and live.is_seeded is False
        assert r.outcomes_for("debtor_live") == []
        r.close()
