"""A reply that says "authorize the link" must contain the link.

Observed live on 2026-09-04, on a real Telegram exchange. The pipeline
diagnosed a two-leg promise, built the plan, issued two real e-mandates,
and the composed reply said:

    "...please authorize the links shared above to set up the debits"

They had not been shared above. They had not been shared anywhere. The
model was handed the links and, instead of listing them, wrote a phrase
referring to a message that did not exist.

This is `docs/WHAT_BROKE.md` #29 on the other path. That fix taught the
*fallback* to carry the links; the composed path -- the one that normally
runs -- was left trusting the model to include them. These tests are the
check that should have existed then.
"""

from __future__ import annotations

import pytest

from agent.api.demo import _ensure_mandate_links_present

ONE = {"mandate_links": [{"mandate_id": "sub_A", "short_url": "https://rzp.io/rzp/AAA111"}]}
TWO = {"mandate_links": [
    {"mandate_id": "sub_A", "short_url": "https://rzp.io/rzp/AAA111"},
    {"mandate_id": "sub_B", "short_url": "https://rzp.io/rzp/BBB222"},
]}


class TestTheLiveFailure:
    def test_a_reply_referring_to_links_it_never_sent_gets_them_appended(self):
        """The exact sentence that shipped."""
        reply = _ensure_mandate_links_present(
            "Confirmed -- Rs 21,000 today and the balance on the 5th; please "
            "authorize the links shared above to set up the debits.", TWO)
        assert "https://rzp.io/rzp/AAA111" in reply
        assert "https://rzp.io/rzp/BBB222" in reply

    def test_it_keeps_what_the_model_wrote(self):
        """Appending, not replacing -- the composed sentence is better than
        anything a template produces, it was just incomplete."""
        original = "Confirmed -- Rs 21,000 today and the balance on the 5th."
        reply = _ensure_mandate_links_present(original, ONE)
        assert reply.startswith(original)

    def test_it_explains_that_no_money_moves_yet(self):
        """The reassurance that makes someone actually click."""
        reply = _ensure_mandate_links_present("Noted.", ONE)
        assert "doesn't take any money now" in reply

    def test_a_partially_complete_reply_only_gains_what_is_missing(self):
        """If the model listed one of two links, don't repeat that one."""
        reply = _ensure_mandate_links_present(
            "Pay the first here: https://rzp.io/rzp/AAA111", TWO)
        assert reply.count("https://rzp.io/rzp/AAA111") == 1
        assert "https://rzp.io/rzp/BBB222" in reply


class TestItLeavesGoodRepliesAlone:
    def test_a_reply_already_carrying_every_link_is_untouched(self):
        good = ("Noted. Authorize these: https://rzp.io/rzp/AAA111 and "
                "https://rzp.io/rzp/BBB222 -- nothing is taken now.")
        assert _ensure_mandate_links_present(good, TWO) == good

    def test_no_plan_means_no_change(self):
        text = "A person will pick this up and get back to you."
        assert _ensure_mandate_links_present(text, None) == text

    def test_a_plan_with_no_mandates_means_no_change(self):
        text = "Understood -- no action needed right now."
        assert _ensure_mandate_links_present(text, {"mandate_links": []}) == text

    def test_a_malformed_link_entry_is_skipped_not_rendered_as_none(self):
        text = "Noted."
        assert _ensure_mandate_links_present(text, {"mandate_links": [{"mandate_id": "x"}]}) == text


class TestWording:
    def test_one_link_reads_singular(self):
        assert "this link" in _ensure_mandate_links_present("Noted.", ONE)

    def test_two_links_read_plural(self):
        assert "these links" in _ensure_mandate_links_present("Noted.", TWO)

    @pytest.mark.parametrize("n", [1, 2, 3, 4])
    def test_every_url_survives_however_many_there_are(self, n):
        plan = {"mandate_links": [
            {"mandate_id": f"sub_{i}", "short_url": f"https://rzp.io/rzp/L{i}"} for i in range(n)]}
        reply = _ensure_mandate_links_present("Noted.", plan)
        for i in range(n):
            assert f"https://rzp.io/rzp/L{i}" in reply
