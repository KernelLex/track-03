"""RazorpayRail, run against the REAL account when credentials are available.
Skipped entirely otherwise -- the only file in the suite that touches the
network, so `pytest` stays free and fast for anyone without test keys.
DEVDOC_v6 §5, §6.
"""

from __future__ import annotations

import os

import pytest

from agent.rails.razorpay_rail import RazorpayRail
from agent.rails.types import InvoiceSpec, LinkSpec, MandateDelta, MandateSpec, OrderSpec, RailUnavailable

KEY_ID = os.environ.get("RAZORPAY_KEY_ID")
KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET")

pytestmark = pytest.mark.skipif(
    not (KEY_ID and KEY_SECRET),
    reason="RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET not set -- these tests hit the live "
           "Razorpay test-mode API and are skipped without real credentials",
)


@pytest.fixture(scope="module")
def rail():
    return RazorpayRail(KEY_ID, KEY_SECRET)


def test_create_order_live(rail):
    order = rail.create_order(OrderSpec(amount_paise=100, receipt="pytest_probe"))
    assert order.id.startswith("order_")
    assert order.amount_paise == 100
    assert order.status == "created"


def test_create_payment_link_live(rail):
    link = rail.create_payment_link(LinkSpec(
        amount_paise=100, description="pytest conformance probe",
        customer_contact="+919123456780", customer_email="pytest@example.com",
    ))
    assert link.id.startswith("plink_")
    assert link.short_url.startswith("https://")
    assert link.amount_paise == 100


def test_create_invoice_live(rail):
    invoice = rail.create_invoice(InvoiceSpec(
        amount_paise=100, description="pytest conformance probe",
        customer_email="pytest@example.com", customer_name="Pytest",
    ))
    assert invoice.id.startswith("inv_")
    assert invoice.amount_paise == 100


def test_create_and_revoke_mandate_live(rail):
    mandate = rail.create_mandate(MandateSpec(
        max_amount_paise=100, start_at="2026-01-01T00:00:00Z", end_at="2027-01-01T00:00:00Z",
    ))
    assert mandate.id.startswith("sub_")
    assert mandate.status == "created"

    revoked = rail.revoke_mandate(mandate.id)
    assert revoked.status == "revoked"


def test_present_debit_raises_rail_unavailable_not_a_guessed_success(rail):
    with pytest.raises(RailUnavailable):
        rail.present_debit("sub_whatever", 100)


def test_modify_mandate_raises_rail_unavailable_not_a_guessed_success(rail):
    with pytest.raises(RailUnavailable):
        rail.modify_mandate("sub_whatever", MandateDelta(max_amount_paise=200))


def test_fetch_order_live_round_trips(rail):
    order = rail.create_order(OrderSpec(amount_paise=100, receipt="pytest_fetch_probe"))
    fetched = rail.fetch("orders", order.id)
    assert fetched["id"] == order.id


def test_fetch_nonexistent_id_raises_rail_unavailable(rail):
    with pytest.raises(RailUnavailable):
        rail.fetch("payments", "pay_doesnotexist123")


def test_fetch_unknown_kind_raises_rail_unavailable(rail):
    with pytest.raises(RailUnavailable):
        rail.fetch("not_a_real_kind", "whatever")


def test_act_executor_dispatches_create_payment_link_against_the_live_rail(tmp_path):
    """The ACT executor (agent/act/executor.py) was only exercised against
    SimulatedRail before this -- proves it's genuinely rail-agnostic, not
    accidentally coupled to SimulatedRail's specific behaviour."""
    from agent.act.actions import ActionType
    from agent.act.executor import OutboundActionStore, execute_action
    from agent.bounds.context import ActionCtx, BoundsContext, ConfigCtx, DebtorCtx, DecisionCtx, InvoiceCtx, MandateCtx

    ctx = BoundsContext(
        debtor=DebtorCtx(id="live_debtor_1", state="ENGAGED"),
        mandate=MandateCtx(),
        action=ActionCtx(type="create_payment_link", channel="email", rail_tag="razorpay"),
        decision=DecisionCtx(ev_paise=1000),
        invoice=InvoiceCtx(id="live_inv_1"),
        config=ConfigCtx(),
    )
    with OutboundActionStore(tmp_path / "outbound.db") as store:
        rail = RazorpayRail(KEY_ID, KEY_SECRET)
        outcome = execute_action(
            action_type=ActionType.CREATE_PAYMENT_LINK, debtor_id="live_debtor_1", invoice_id="live_inv_1",
            decision_seq=1, bounds_context=ctx, rail=rail, outbound_store=store,
            payload={"amount_paise": 100, "description": "ACT executor live probe"},
        )
    assert outcome.external_ref.startswith("plink_")
    assert outcome.rail_tag == "razorpay"


def test_razorpay_rail_passes_the_shared_conformance_suite():
    """§5.4's actual claim: the SAME suite that passes against SimulatedRail
    (tests/agent/test_conformance.py) also passes against the real rail --
    not two different notions of "conforms"."""
    from agent.rails.conformance.suite import run_conformance_suite

    report = run_conformance_suite(lambda secret: RazorpayRail(KEY_ID, KEY_SECRET))
    assert report.rail_tag == "razorpay"
    assert report.all_passed, [(c.name, c.detail) for c in report.failures]
