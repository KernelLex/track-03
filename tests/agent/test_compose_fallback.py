"""What the agent says when the composer fails.

This exists because of a specific live failure on 2026-09-02, on the first
real WhatsApp exchange after inbound was wired up. The debtor said "I can
pay 21,000 on the 5th and the rest by month end". Everything worked:
PROMISE_STATED at 0.88, the plan built, and a real Razorpay e-mandate
issued (`sub_TXBeQY5swx95DX`, `rzp.io/rzp/0XUkcMiB`).

Then `compose_reply` returned no text, and the fallback -- which knew only
the diagnosis family -- sent:

    "Understood -- no rush, it'll confirm itself once it's paid."

The debtor was never given the authorization link the agent had just
created for them, and was told there was nothing to do. A fallback is
allowed to be bland. It is not allowed to withhold the artifact the
exchange produced, or to imply no action is needed when a signature is.
"""

from __future__ import annotations

import pytest

from agent.api.demo import _agent_reply_for
from agent.diagnose.extract import Family

MANDATE = [{"mandate_id": "sub_TXBeQY5swx95DX", "short_url": "https://rzp.io/rzp/0XUkcMiB",
            "amount_paise": 2100000}]
SECOND = {"mandate_id": "sub_SECOND", "short_url": "https://rzp.io/rzp/2ndLink",
          "amount_paise": 2150000}


class TestTheLiveFailure:
    def test_an_issued_mandate_link_reaches_the_debtor(self):
        """The exact regression. If this ever fails again, someone is being
        asked to authorize something they were never sent."""
        reply = _agent_reply_for(Family.C, mandate_links=MANDATE)
        assert "https://rzp.io/rzp/0XUkcMiB" in reply

    def test_it_no_longer_says_there_is_nothing_to_do(self):
        """"no rush, it'll confirm itself once it's paid" is false when a
        mandate is waiting for a signature."""
        reply = _agent_reply_for(Family.C, mandate_links=MANDATE)
        assert "no rush" not in reply.lower()
        assert "confirm itself" not in reply.lower()

    def test_it_says_what_the_link_is_for(self):
        reply = _agent_reply_for(Family.C, mandate_links=MANDATE)
        assert "authorize" in reply.lower()

    def test_it_reassures_that_no_money_moves_yet(self):
        """A debtor asked to authorize a debit needs to know the click does
        not take the money now -- the same reassurance the composed reply
        gives, and the reason someone actually clicks."""
        reply = _agent_reply_for(Family.C, mandate_links=MANDATE)
        assert "doesn't take any money now" in reply


class TestSeveralLegs:
    def test_both_links_are_carried(self):
        reply = _agent_reply_for(Family.C, mandate_links=[*MANDATE, SECOND])
        assert "https://rzp.io/rzp/0XUkcMiB" in reply
        assert "https://rzp.io/rzp/2ndLink" in reply

    def test_the_wording_is_plural(self):
        reply = _agent_reply_for(Family.C, mandate_links=[*MANDATE, SECOND])
        assert "these links" in reply

    def test_a_single_leg_stays_singular(self):
        assert "this link" in _agent_reply_for(Family.C, mandate_links=MANDATE)


class TestItDoesNotOverreach:
    def test_no_mandate_means_the_old_bland_line(self):
        """Blandness is correct when nothing was produced -- the bug was
        blandness in the presence of something actionable, not blandness."""
        reply = _agent_reply_for(Family.C, mandate_links=None)
        assert "no rush" in reply.lower()

    def test_an_empty_list_is_treated_as_no_mandate(self):
        assert "no rush" in _agent_reply_for(Family.C, mandate_links=[]).lower()

    def test_a_link_entry_with_no_url_is_skipped_not_rendered_as_none(self):
        """A malformed entry must not produce "authorize this link: None"."""
        reply = _agent_reply_for(Family.C, mandate_links=[{"mandate_id": "sub_X"}])
        assert "None" not in reply
        assert "no rush" in reply.lower()

    @pytest.mark.parametrize("family,expected", [
        (Family.A, "repair"),
        (Family.B, "pausing automated contact"),
        (Family.D, "needs a person"),
    ])
    def test_other_families_are_untouched(self, family, expected):
        """A mandate link is a Family C artifact. Passing one must not
        rewrite the dispute or blocker replies -- Family D in particular
        must keep saying a human is taking over, never hand out a link."""
        reply = _agent_reply_for(family, mandate_links=MANDATE)
        assert expected in reply
        assert "rzp.io" not in reply

    def test_a_dispute_never_receives_a_payment_link(self):
        """The costly one. Family D means the debtor disputes the debt;
        answering that with 'authorize this debit' is the RBI Fair
        Practices problem the whole bounds gate exists to prevent."""
        assert "authorize" not in _agent_reply_for(Family.D, mandate_links=MANDATE).lower()
