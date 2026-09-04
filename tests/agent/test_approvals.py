"""The queue a human works from when the agent escalates.

`escalate_human` used to be a decision with nowhere to go: the agent
correctly refused to act, told the debtor a person would pick it up, and no
person could -- there was no list of what was waiting. An escalation that
lands nowhere is only half a safety property. The gate stopped the wrong
thing; the right thing never happened either.
"""

from __future__ import annotations

import pytest

from agent.api.approvals import APPROVED, PENDING, REJECTED, ApprovalQueue


@pytest.fixture
def queue(tmp_path):
    with ApprovalQueue(str(tmp_path / "approvals.db")) as q:
        yield q


def _open(q, **kw):
    base = dict(conversation_id="8327566456", channel="telegram", reason="NOT_OUR_DEBT",
                debtor_label="8327566456", invoice_id="INV-2201",
                refusals=["DISPUTE_FREEZE"], debtor_said="this isn't our debt",
                proposed_message="A person will pick this up.")
    base.update(kw)
    return q.open_for(**base)


class TestFilingAnEscalation:
    def test_an_escalation_becomes_something_a_human_can_see(self, queue):
        _open(queue)
        assert len(queue.pending()) == 1

    def test_it_carries_what_the_debtor_actually_said(self, queue):
        """A human deciding without the debtor's own words is deciding
        blind."""
        item = queue.get(_open(queue))
        assert item["debtor_said"] == "this isn't our debt"
        assert item["refusals"] == ["DISPUTE_FREEZE"]

    def test_it_carries_the_message_the_agent_would_have_sent(self, queue):
        """So approving is one click and something real goes out, rather
        than approving an abstraction and then having to write it."""
        assert queue.get(_open(queue))["proposed_message"] == "A person will pick this up."

    def test_a_second_escalation_on_the_same_conversation_does_not_queue_twice(self, queue):
        """A debtor who writes three times while waiting must not produce
        three identical rows -- that is how a queue becomes noise."""
        first = _open(queue)
        second = _open(queue, debtor_said="hello? anyone there")
        assert first == second
        assert len(queue.pending()) == 1

    def test_re_escalating_refreshes_the_details(self, queue):
        _open(queue)
        _open(queue, debtor_said="hello? anyone there")
        assert queue.pending()[0]["debtor_said"] == "hello? anyone there"

    def test_different_conversations_queue_separately(self, queue):
        _open(queue)
        _open(queue, conversation_id="+919611550053", channel="whatsapp")
        assert len(queue.pending()) == 2


class TestDeciding:
    def test_approving_resolves_it(self, queue):
        item_id = _open(queue)
        assert queue.decide(item_id, decision=APPROVED)["status"] == APPROVED
        assert queue.pending() == []

    def test_rejecting_resolves_it_too(self, queue):
        item_id = _open(queue)
        assert queue.decide(item_id, decision=REJECTED)["status"] == REJECTED
        assert queue.pending() == []

    def test_a_decision_is_not_re_decidable(self, queue):
        """Two people clicking approve must not send twice."""
        item_id = _open(queue)
        queue.decide(item_id, decision=APPROVED)
        assert queue.decide(item_id, decision=REJECTED) is None

    def test_the_internal_note_is_kept(self, queue):
        item_id = _open(queue)
        queue.decide(item_id, decision=REJECTED, note="spoke to them, terms not viable")
        assert queue.get(item_id)["decided_note"] == "spoke to them, terms not viable"

    def test_an_unknown_decision_is_refused(self, queue):
        with pytest.raises(ValueError):
            queue.decide(_open(queue), decision="maybe")

    def test_deciding_something_that_does_not_exist_returns_none(self, queue):
        assert queue.decide("nope", decision=APPROVED) is None

    def test_resolving_frees_the_conversation_for_a_new_escalation(self, queue):
        """The one-open-per-conversation rule must not permanently block a
        debtor who escalates again next week."""
        first = _open(queue)
        queue.decide(first, decision=APPROVED)
        assert _open(queue) != first
        assert len(queue.pending()) == 1


class TestTheSendIsRecordedAgainstTheDecision:
    def test_a_successful_send_is_recorded(self, queue):
        item_id = _open(queue)
        queue.decide(item_id, decision=APPROVED)
        queue.record_send(item_id, ref="MM123")
        assert queue.get(item_id)["sent_ref"] == "MM123"

    def test_a_failed_send_is_recorded_rather_than_lost(self, queue):
        """A decision that was made but never reached the debtor is the
        state most worth being able to see afterwards."""
        item_id = _open(queue)
        queue.decide(item_id, decision=APPROVED)
        queue.record_send(item_id, ref=None, error="ChannelUnavailable: telegram")
        item = queue.get(item_id)
        assert item["status"] == APPROVED
        assert item["send_error"].startswith("ChannelUnavailable")

    def test_the_decision_survives_independently_of_the_send(self, queue):
        """Recorded before sent, deliberately: a crash between the two must
        leave the decision on record, not a contacted debtor with no
        record of who authorised it."""
        item_id = _open(queue)
        queue.decide(item_id, decision=APPROVED)
        assert queue.get(item_id)["decided_at"] is not None
        assert queue.get(item_id)["sent_ref"] is None


class TestHousekeeping:
    def test_recent_shows_resolved_items(self, queue):
        item_id = _open(queue)
        queue.decide(item_id, decision=REJECTED)
        assert [i["status"] for i in queue.recent()] == [REJECTED]

    def test_clear_empties_the_queue(self, queue):
        _open(queue)
        _open(queue, conversation_id="other")
        assert queue.clear() == 2
        assert queue.pending() == []

    def test_it_survives_a_reopen(self, tmp_path):
        """Each request opens a fresh handle, so in-memory state would lose
        the queue between the escalation and the human seeing it."""
        path = str(tmp_path / "a.db")
        with ApprovalQueue(path) as q:
            _open(q)
        with ApprovalQueue(path) as q:
            assert len(q.pending()) == 1
            assert q.pending()[0]["status"] == PENDING
