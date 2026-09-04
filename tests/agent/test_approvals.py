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


class TestApprovingSendsSomethingActionable:
    """The reason the queue exists at all.

    The agent's escalation text is "...and a person will confirm this with
    you directly." Approving that and sending it unchanged tells the debtor
    a *second* time that someone will get back to them -- which is not an
    outcome, it is the same non-answer with a human's signature on it.

    So whatever goes out on approval carries the mandate links, whether it
    came from the agent or the human typed it themselves.
    """

    LINKS = [{"mandate_id": "sub_A", "short_url": "https://rzp.io/rzp/AAA111"},
             {"mandate_id": "sub_B", "short_url": "https://rzp.io/rzp/BBB222"}]

    def test_the_links_ride_along_on_the_approval(self, queue):
        item_id = _open(queue, mandate_links=self.LINKS)
        assert len(queue.get(item_id)["mandate_links"]) == 2

    def test_the_agents_escalation_text_gains_the_links(self):
        """The exact message the user saw, put through the same guard the
        approve endpoint applies."""
        from agent.api.demo import _ensure_mandate_links_present
        sent = _ensure_mandate_links_present(
            "Noted -- full payment of Rs 42,500 on the 5th as you've mentioned, "
            "and a person will confirm this with you directly.",
            {"mandate_links": self.LINKS})
        assert "https://rzp.io/rzp/AAA111" in sent
        assert "https://rzp.io/rzp/BBB222" in sent

    def test_a_humans_own_wording_also_gains_them(self):
        """Someone typing their own message should not have to remember to
        paste the links -- forgetting is the failure mode, not laziness."""
        from agent.api.demo import _ensure_mandate_links_present
        sent = _ensure_mandate_links_present(
            "Happy to go ahead with that.", {"mandate_links": self.LINKS})
        assert "https://rzp.io/rzp/AAA111" in sent

    def test_no_links_on_the_approval_means_the_text_is_untouched(self):
        from agent.api.demo import _ensure_mandate_links_present
        text = "A colleague reviewed this and approved it."
        assert _ensure_mandate_links_present(text, {"mandate_links": []}) == text

    def test_links_survive_a_re_escalation(self, queue):
        """Refreshing an open row must not drop them."""
        _open(queue, mandate_links=self.LINKS)
        item_id = _open(queue, debtor_said="any update?", mandate_links=self.LINKS)
        assert len(queue.get(item_id)["mandate_links"]) == 2


class TestSchemaMigration:
    """A column added after the table shipped must reach existing databases.

    `CREATE TABLE IF NOT EXISTS` is a no-op against an existing table, so a
    new column reaches a fresh database and silently misses every deployed
    one -- surfacing as a crash on the next query, in production, on a file
    that worked a moment earlier. That is exactly what happened: a leftover
    db from an earlier run started raising `no such column: mandate_links`,
    and the deployed instance had the same stale schema.
    """

    def test_an_old_database_gains_the_new_column(self, tmp_path):
        import sqlite3
        path = str(tmp_path / "old.db")
        # The schema as it shipped, without mandate_links.
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE approvals (id TEXT PRIMARY KEY, created_at REAL NOT NULL,"
            " conversation_id TEXT NOT NULL, channel TEXT NOT NULL, debtor_label TEXT,"
            " invoice_id TEXT, reason TEXT NOT NULL, refusals TEXT, debtor_said TEXT,"
            " proposed_message TEXT, status TEXT NOT NULL DEFAULT 'pending',"
            " decided_at REAL, decided_note TEXT, sent_ref TEXT, send_error TEXT)")
        conn.commit()
        conn.close()

        with ApprovalQueue(path) as q:
            item_id = _open(q, mandate_links=[{"short_url": "https://rzp.io/rzp/X"}])
            assert q.get(item_id)["mandate_links"] == [{"short_url": "https://rzp.io/rzp/X"}]

    def test_rows_already_in_an_old_database_still_read(self, tmp_path):
        """Existing escalations must survive the migration, not vanish."""
        import sqlite3
        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE approvals (id TEXT PRIMARY KEY, created_at REAL NOT NULL,"
            " conversation_id TEXT NOT NULL, channel TEXT NOT NULL, debtor_label TEXT,"
            " invoice_id TEXT, reason TEXT NOT NULL, refusals TEXT, debtor_said TEXT,"
            " proposed_message TEXT, status TEXT NOT NULL DEFAULT 'pending',"
            " decided_at REAL, decided_note TEXT, sent_ref TEXT, send_error TEXT)")
        conn.execute("INSERT INTO approvals (id, created_at, conversation_id, channel, reason)"
                     " VALUES ('old1', 1.0, 'c1', 'telegram', 'DISPUTE')")
        conn.commit()
        conn.close()

        with ApprovalQueue(path) as q:
            pending = q.pending()
            assert [p["id"] for p in pending] == ["old1"]
            assert pending[0]["mandate_links"] == []

    def test_migrating_twice_is_harmless(self, tmp_path):
        path = str(tmp_path / "a.db")
        with ApprovalQueue(path):
            pass
        with ApprovalQueue(path) as q:
            assert q.pending() == []


class TestWhatAHumanGetsToSend:
    """An approval with nothing attached is a human clicking "approve" and
    the debtor receiving another sentence with nothing to do about it.

    The live case: a debtor wrote "I can pay everything on 5th", the gate
    refused the reminder on PROMISE_COOLDOWN, no plan was built (a bare date
    against an outstanding plan is deliberately read as a *change* to it),
    and the reply said "a person will confirm this with you directly" --
    with no link and nobody queued.
    """

    def test_a_plans_mandates_are_what_gets_sent(self):
        from agent.api.demo import _links_for_approval
        plan = {"mandate_links": [{"short_url": "https://rzp.io/rzp/AAA"},
                                  {"short_url": "https://rzp.io/rzp/BBB"}]}
        assert len(_links_for_approval(plan, {})) == 2

    def test_no_plan_falls_back_to_the_invoice_url(self, monkeypatch):
        """The floor: they always get a way to pay."""
        import agent.api.demo as demo
        monkeypatch.setattr(demo, "_create_real_payment_link", lambda s: "https://rzp.io/rzp/INV")
        links = demo._links_for_approval(None, {"invoice_id": "INV-2201"})
        assert [l["short_url"] for l in links] == ["https://rzp.io/rzp/INV"]

    def test_no_plan_and_no_payable_url_yields_nothing_rather_than_a_broken_link(self, monkeypatch):
        """A `None` rendered into a message is worse than no link."""
        import agent.api.demo as demo
        monkeypatch.setattr(demo, "_create_real_payment_link", lambda s: None)
        assert demo._links_for_approval(None, {}) == []

    def test_the_fallback_is_not_used_when_a_plan_exists(self, monkeypatch):
        """Sending the whole invoice alongside an agreed instalment plan
        would contradict the plan."""
        import agent.api.demo as demo
        monkeypatch.setattr(demo, "_create_real_payment_link",
                            lambda s: (_ for _ in ()).throw(AssertionError("should not be called")))
        plan = {"mandate_links": [{"short_url": "https://rzp.io/rzp/AAA"}]}
        assert len(demo._links_for_approval(plan, {})) == 1
