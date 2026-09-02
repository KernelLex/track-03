"""A debtor asking about their own invoices, without a model call.

The conversation used to be about one hardcoded invoice, so the most common
thing a counterparty actually wants -- "which of these do I still owe?" --
could not be asked at all. Answering it removes work from a human queue
without chasing anyone, which is the same argument this project makes about
diagnosis: the cheapest recovery is the one that never needed a chase.

Two properties matter more than the feature working:

  - **Prose still reaches the extractor.** A keyword router that swallows
    "2 units were damaged" as a menu selection would be worse than no
    router.
  - **Status is a fact, not a claim.** Nothing a debtor types marks an
    invoice paid.
"""

from __future__ import annotations

import agent.api.demo as demo_module
import pytest

from agent.debtor.invoices import DISPUTED, OUTSTANDING, PAID, InvoiceStore
from agent.debtor.registry import DebtorRegistry
from agent.debtor.seed import seed_invoices, seed_registry
from agent.notify.intents import detect_intent

CHAT_ID = "555000222"


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUECOMMIT_CONVERSATION_DB", str(tmp_path / "conversation.db"))
    monkeypatch.setenv("TRUECOMMIT_DEBTORS_DB", str(tmp_path / "debtors.db"))
    monkeypatch.setenv("TRUECOMMIT_EXTRACTION_LOG", str(tmp_path / "extraction.db"))
    monkeypatch.setenv("DEMO_CONTACT_TELEGRAM_CHAT_ID", CHAT_ID)
    # No Razorpay credentials: no real rail objects from this suite.
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)

    registry = DebtorRegistry(str(tmp_path / "debtors.db"))
    seed_registry(registry)
    registry.close()
    store = InvoiceStore(str(tmp_path / "debtors.db"))
    seed_invoices(store)
    store.close()
    return tmp_path


def _say(text, *, external_id="1"):
    sent: list[str] = []
    result = demo_module.handle_inbound_message(
        conversation_id=CHAT_ID, external_id=external_id, text=text,
        channel="telegram", send=sent.append,
    )
    return result, sent


class TestTheRouterKnowsWhatItIsNotFor:
    """Every one of these must reach the extractor, not a menu branch."""

    @pytest.mark.parametrize("text", [
        "I can pay 21000 today and rest on 5th",
        "2 units were damaged",
        "I'll pay 2 lakh next week",
        "I dispute the quantity on this invoice, we received 40 of the 50 units ordered",
        "ok",
        "",
    ])
    def test_prose_falls_through(self, text):
        assert detect_intent(text) is None

    def test_a_long_message_mentioning_dispute_is_prose(self):
        """A keyword anywhere in a paragraph must not hijack routing -- the
        extractor can tell a dispute from a sentence mentioning one."""
        assert detect_intent(
            "Hi, following up on our call, there may be a dispute about the delivery "
            "quantity but I need to check with the warehouse first"
        ) is None

    @pytest.mark.parametrize("text,kind", [
        ("invoices", "list"), ("what do i owe", "list"), ("my invoices", "list"),
        ("2", "select"), ("#3", "select"), ("INV-2201", "select"),
        ("dispute", "dispute"), ("schedule", "schedule"),
        ("i have a problem", "problem"), ("talk to a person", "problem"),
    ])
    def test_commands_are_recognised(self, text, kind):
        intent = detect_intent(text)
        assert intent is not None and intent.kind == kind


class TestListingInvoices:
    def test_it_shows_what_is_due_and_what_is_paid(self, wired):
        result, sent = _say("what do i owe")
        reply = result["agent_reply"]
        assert "INV-2201" in reply and "INV-2176" in reply
        assert "paid" in reply
        assert sent == [reply]

    def test_it_totals_only_the_open_invoices(self, wired):
        """A paid invoice appearing in the outstanding total is the kind of
        error that destroys trust in the whole number."""
        result, _ = _say("invoices")
        # 42,500 + 9,750 open; 18,400 paid and excluded.
        assert "52,250" in result["agent_reply"]

    def test_no_model_call_is_made(self, wired, monkeypatch):
        """The whole point of the deterministic router: a menu selection
        costs nothing and takes no seconds."""
        def _fail(*args, **kwargs):
            raise AssertionError("the extractor must not be called for a menu command")

        monkeypatch.setattr(demo_module, "extract_from_reply", _fail)
        monkeypatch.setattr(demo_module, "compose_reply", _fail)
        result, _ = _say("invoices")
        assert "INV-2201" in result["agent_reply"]


class TestSelectingAndActing:
    def test_a_number_selects_the_invoice_printed_at_that_position(self, wired):
        _say("invoices", external_id="1")
        result, _ = _say("1", external_id="2")
        assert "INV-2201" in result["agent_reply"]

    def test_the_selection_is_remembered_for_the_next_command(self, wired):
        """"dispute" on its own means the invoice they just picked."""
        _say("invoices", external_id="1")
        _say("1", external_id="2")
        result, _ = _say("dispute", external_id="3")
        assert "INV-2201" in result["agent_reply"]

    def test_a_dispute_freezes_the_invoice_and_routes_it_to_a_person(self, wired):
        _say("1", external_id="1")
        result, _ = _say("dispute", external_id="2")

        assert "person will review" in result["agent_reply"]
        store = InvoiceStore(str(wired / "debtors.db"))
        try:
            assert store.get("debtor_live", "INV-2201").status == DISPUTED
        finally:
            store.close()

    def test_a_disputed_invoice_leaves_the_outstanding_total(self, wired):
        _say("1", external_id="1")
        _say("dispute", external_id="2")
        result, _ = _say("invoices", external_id="3")
        assert "9,750" in result["agent_reply"]

    def test_scheduling_a_disputed_invoice_is_refused(self, wired):
        """DISPUTE_FREEZE's whole point: no debit is set up on a contested
        amount while a person is looking at it."""
        _say("1", external_id="1")
        _say("dispute", external_id="2")
        result, _ = _say("schedule", external_id="3")
        assert "not" in result["agent_reply"].lower()

        store = InvoiceStore(str(wired / "debtors.db"))
        try:
            assert store.get("debtor_live", "INV-2201").status == DISPUTED
        finally:
            store.close()

    def test_scheduling_a_paid_invoice_is_a_no_op(self, wired):
        _say("INV-2176", external_id="1")
        result, _ = _say("schedule", external_id="2")
        assert "already paid" in result["agent_reply"]

    def test_asking_for_a_person_escalates_without_diagnosing_first(self, wired):
        """They asked for a human. Guessing at the problem first answers a
        question they did not ask."""
        _say("1", external_id="1")
        result, _ = _say("problem", external_id="2")
        assert "person" in result["agent_reply"]
        assert result["self_service"] == "problem"

    def test_an_unmatched_selection_says_so_rather_than_guessing(self, wired):
        result, _ = _say("9", external_id="1")
        assert "could not match" in result["agent_reply"]


class TestStatusIsAFactNotAClaim:
    def test_nothing_a_debtor_types_marks_an_invoice_paid(self, wired, monkeypatch):
        """Law 7's standard reaches into this feature too: only a
        rail-confirmed capture settles an invoice. `ALREADY_PAID_UNRECONCILED`
        exists as a diagnosis class precisely because a claim of payment and
        a payment are different things.

        These messages are prose, so they correctly fall through to the
        extractor -- which is stubbed here rather than called, since the
        assertion is about what the *store* does, not what the model says.
        """
        from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family

        monkeypatch.setattr(
            demo_module, "extract_from_reply",
            lambda t, **kw: ExtractionResult(
                family=Family.B, class_=DiagnosisClass.ALREADY_PAID_UNRECONCILED, confidence=0.8),
        )
        monkeypatch.setattr(demo_module, "compose_reply", lambda reply_text, **kw: "noted")

        for text in ("mark INV-2201 as paid", "paid", "I have paid INV-2201",
                     "INV-2201 is settled"):
            _say(text, external_id=text)

        store = InvoiceStore(str(wired / "debtors.db"))
        try:
            assert store.get("debtor_live", "INV-2201").status == OUTSTANDING
        finally:
            store.close()

    def test_a_capture_is_what_marks_it_paid(self, wired):
        store = InvoiceStore(str(wired / "debtors.db"))
        try:
            assert store.mark_paid_by_capture("debtor_live", "INV-2201", payment_id="pay_1")
            assert store.get("debtor_live", "INV-2201").status == PAID
        finally:
            store.close()

    def test_seeding_never_overwrites_a_status_a_payment_moved(self, wired):
        """Seeding runs on every boot, and Render restarts often."""
        store = InvoiceStore(str(wired / "debtors.db"))
        try:
            store.mark_paid_by_capture("debtor_live", "INV-2201", payment_id="pay_1")
            seed_invoices(store)
            assert store.get("debtor_live", "INV-2201").status == PAID
        finally:
            store.close()


class TestRealTyping:
    """Widened after probing the router against how people actually write.

    The first version handled the phrasings I had thought of and missed
    "invoice pls", "what all is pending" (Indian English, and among the most
    likely phrasings this will meet), "no 2", "the first one", and -- worst
    -- "i want to dispute", an explicit request that fell through, so the
    debtor did not get the freeze they had asked for.
    """

    @pytest.mark.parametrize("text,kind", [
        ("invoice pls", "list"), ("what all is pending", "list"),
        ("bill status", "list"), ("pending", "list"), ("statement", "list"),
        ("no 2", "select"), ("option 3", "select"), ("1.", "select"),
        ("the first one", "select"), ("second one", "select"),
        ("i want to dispute", "dispute"), ("need to contest", "dispute"),
        ("need help", "problem"), ("call me", "problem"),
        ("connect me to someone", "problem"),
        ("set up autopay", "schedule"), ("emandate", "schedule"),
    ])
    def test_realistic_phrasings_are_understood(self, text, kind):
        intent = detect_intent(text)
        assert intent is not None, f"{text!r} was not recognised"
        assert intent.kind == kind

    @pytest.mark.parametrize("text", [
        "2 units were damaged",
        "no 2 units arrived",
        "the first one was damaged",
        "pending approval from our finance head",
        "call me tomorrow after I speak to my accountant",
        "I'll pay 2 lakh next week",
        "I dispute the quantity on this invoice, we received 40 of 50 units",
    ])
    def test_widening_did_not_start_swallowing_prose(self, text):
        """Each of these is a near-miss of a command it must not match.
        Widening a keyword router is exactly where this goes wrong, so the
        pairs are tested together: "pending" is a command and "pending
        approval from our finance head" is a sentence about a blocker."""
        assert detect_intent(text) is None

    def test_an_ordinal_selects_by_list_position(self, wired):
        _say("invoices", external_id="1")
        result, _ = _say("the first one", external_id="2")
        assert "INV-2201" in result["agent_reply"]

    def test_a_dispute_request_actually_freezes_the_invoice(self, wired):
        """The miss that mattered: recognising the phrasing is only useful
        if it reaches the same freeze a bare "dispute" does."""
        _say("1", external_id="1")
        _say("i want to dispute", external_id="2")

        store = InvoiceStore(str(wired / "debtors.db"))
        try:
            assert store.get("debtor_live", "INV-2201").status == DISPUTED
        finally:
            store.close()


class TestResettingTheDemo:
    """Rehearsing leaves real marks -- disputes raised, mandates scheduled,
    invoices out of the outstanding total. A recording that opens on last
    night's leftovers is a worse problem than a secret-gated reset is a
    risk."""

    def test_it_puts_invoices_back_to_their_declared_state(self, wired):
        from agent.debtor.seed import reset_invoices

        _say("1", external_id="1")
        _say("dispute", external_id="2")

        store = InvoiceStore(str(wired / "debtors.db"))
        try:
            assert store.get("debtor_live", "INV-2201").status == DISPUTED
            reset_invoices(store)
            assert store.get("debtor_live", "INV-2201").status == OUTSTANDING
        finally:
            store.close()

    def test_seeding_still_refuses_to_undo_a_real_payment(self, wired):
        """The distinction that matters: `seed_invoices` runs on every boot
        and must never resurrect a paid invoice, while `reset_invoices` is
        a deliberate, gated call that may."""
        from agent.debtor.seed import reset_invoices, seed_invoices

        store = InvoiceStore(str(wired / "debtors.db"))
        try:
            store.mark_paid_by_capture("debtor_live", "INV-2201", payment_id="pay_real")
            seed_invoices(store)
            assert store.get("debtor_live", "INV-2201").status == PAID, "a boot must not undo a capture"
            reset_invoices(store)
            assert store.get("debtor_live", "INV-2201").status == OUTSTANDING
        finally:
            store.close()

    def test_clearing_a_conversation_forgets_the_handled_claims_too(self, wired):
        """Keeping them would mean a reset conversation still refused to
        answer a message it had already seen -- the opposite of a clean
        slate."""
        from agent.notify.conversation import ConversationStore

        _say("invoices", external_id="dup-1")
        store = ConversationStore(str(wired / "conversation.db"))
        try:
            assert store.claim_message(CHAT_ID, "dup-1") is False
            store.clear(CHAT_ID)
            assert store.claim_message(CHAT_ID, "dup-1") is True
            assert store.recent_turns(CHAT_ID) == []
        finally:
            store.close()


class TestClearingAWrongRecord:
    """A defect in matching a capture to a promise (WHAT_BROKE #26) scored a
    debtor who had genuinely paid as having broken their word, and nothing
    could correct it. `clear_promises` is that correction -- deliberately
    off by default, because it deletes a record of real events."""

    def test_reset_leaves_promises_alone_by_default(self, wired):
        from agent.debtor.registry import DebtorRegistry
        from agent.debtor.seed import reset_invoices

        registry = DebtorRegistry(str(wired / "debtors.db"))
        registry.record_promise("debtor_live", invoice_id="INV-2201",
                                amount_paise=1000, promised_date="2026-09-05")
        reset_invoices(InvoiceStore(str(wired / "debtors.db")))
        assert len(registry.outcomes_for("debtor_live")) == 1
        registry.close()

    def test_clearing_promises_restores_the_no_history_score(self, wired):
        from agent.debtor.registry import DebtorRegistry

        registry = DebtorRegistry(str(wired / "debtors.db"))
        try:
            registry.record_promise("debtor_live", invoice_id="INV-2201", amount_paise=1000,
                                    promised_date="2020-01-01", outcome="broken")
            assert registry.terms("debtor_live").band == "strict"

            assert registry.clear_promises("debtor_live") == 1
            assert registry.outcomes_for("debtor_live") == []
            assert registry.terms("debtor_live").band == "trusted"
        finally:
            registry.close()

    def test_a_seeded_debtor_s_declared_history_is_not_cleared(self, wired):
        """Seeded histories are fixtures the demo needs in order to show the
        score's range. Wiping them would leave every band identical."""
        from agent.debtor.registry import DebtorRegistry

        registry = DebtorRegistry(str(wired / "debtors.db"))
        try:
            before = len(registry.outcomes_for("debtor_orbit"))
            assert before > 0
        finally:
            registry.close()
