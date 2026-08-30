"""DEVDOC_v6 §9.5: "Replay the event stream three times, shuffled. Assert identical
final state, identical recovery_ledger contents, identical total. That test is Law
7's proof." Run against the real pipeline — SimulatedRail's webhooks through
verify_and_ingest (redelivery defense) into RecoveryLedger.attribute (double-
attribution defense) — not a synthetic stand-in for either.
"""

from __future__ import annotations

import random

from agent.ingest.webhooks import EventStore, verify_and_ingest
from agent.ledger.recovery import RecoveryLedger
from agent.rails.simulated import SimulatedRail
from agent.rails.types import LinkSpec

SECRET = "shuffle-test-secret"


def _build_event_stream() -> tuple[list, dict[str, str]]:
    """Five payment links get paid; every delivery is duplicated once, the way a
    real webhook provider's at-least-once retry actually behaves."""
    rail = SimulatedRail(webhook_secret=SECRET)
    debtor_for_link: dict[str, str] = {}
    for i in range(5):
        link = rail.create_payment_link(LinkSpec(amount_paise=10_000 * (i + 1), description=f"Invoice {i}"))
        debtor_for_link[link.id] = f"debtor_{i % 3}"
        rail.simulate_link_paid(link.id)

    stream = list(rail.emitted_webhooks) + list(rail.emitted_webhooks)
    return stream, debtor_for_link


def _run_pipeline(stream: list, debtor_for_link: dict[str, str], tmp_path, run_id: int) -> tuple[int, set[str]]:
    with EventStore(tmp_path / f"events_{run_id}.db") as store, \
         RecoveryLedger(tmp_path / f"recovery_{run_id}.db") as recovery:
        for webhook in stream:
            result = verify_and_ingest(
                store=store, source="simulated", body=webhook.body,
                signature=webhook.signature, secret=SECRET,
            )
            if result.is_duplicate or result.event_type != "payment_link.paid":
                continue
            link_entity = result.payload["payment_link"]["entity"]
            payment_entity = result.payload["payment"]["entity"]
            recovery.attribute(
                payment_id=payment_entity["id"],
                payment_status=payment_entity["status"],
                invoice_id=link_entity["id"],
                debtor_id=debtor_for_link[link_entity["id"]],
                amount_paise=payment_entity["amount"],
                rail_tag="simulated",
            )
        total = recovery.total_recovered_paise()
        payment_ids = {e.payment_id for e in recovery.all_entries()}
        return total, payment_ids


def test_shuffled_thrice_replay_is_idempotent_and_order_independent(tmp_path):
    stream, debtor_for_link = _build_event_stream()
    assert len(stream) == 10  # 5 links x 1 webhook each (payment_link.paid) x 2 (duplicated)

    results = []
    for run_id in range(3):
        shuffled = list(stream)
        random.shuffle(shuffled)
        results.append(_run_pipeline(shuffled, debtor_for_link, tmp_path, run_id))

    totals = {total for total, _ in results}
    payment_id_sets = [ids for _, ids in results]

    assert len(totals) == 1, f"recovered total differed across shuffles: {totals}"
    assert all(ids == payment_id_sets[0] for ids in payment_id_sets), "attributed payment_ids differed across shuffles"

    expected_total = sum(10_000 * (i + 1) for i in range(5))
    assert totals.pop() == expected_total
    assert len(payment_id_sets[0]) == 5  # one payment_id per link, redeliveries did not double-count
