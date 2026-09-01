"""INGEST stage: verify-before-parse ordering, and the redelivery defense. DEVDOC_v6 §9.2, §9.3."""

from __future__ import annotations

import json

import pytest

from agent.ingest.webhooks import EventStore, IngestResult, MalformedWebhook, SignatureInvalid, verify_and_ingest
from agent.rails.webhook_signing import sign

SECRET = "shared-secret"


@pytest.fixture
def store(tmp_path):
    with EventStore(tmp_path / "events.db") as s:
        yield s


def make_body(event: str = "payment.captured", event_id: str = "evt_1", payload: dict | None = None) -> bytes:
    envelope = {"event": event, "event_id": event_id, "payload": payload or {}}
    return json.dumps(envelope, sort_keys=True).encode("utf-8")


def test_real_razorpay_shaped_body_with_no_event_id_field_uses_the_header_instead(store):
    """Razorpay's real webhook body has no top-level `event_id` -- only
    `event`/`payload` (verified against Razorpay's own webhook docs). The
    real event id arrives solely via the `x-razorpay-event-id` header,
    which the route must pass through as `event_id_header`."""
    envelope = {"entity": "event", "account_id": "acc_1", "event": "payment.failed", "payload": {"payment": {"entity": {"id": "pay_1"}}}}
    body = json.dumps(envelope, sort_keys=True).encode("utf-8")
    signature = sign(body, SECRET)

    result = verify_and_ingest(
        store=store, source="razorpay", body=body, signature=signature, secret=SECRET,
        event_id_header="evt_from_header",
    )

    assert result.event_id == "evt_from_header"
    assert result.event_type == "payment.failed"


def test_no_header_and_no_body_event_id_still_raises_malformed(store):
    envelope = {"event": "payment.failed", "payload": {}}
    body = json.dumps(envelope, sort_keys=True).encode("utf-8")
    signature = sign(body, SECRET)
    with pytest.raises(MalformedWebhook):
        verify_and_ingest(store=store, source="razorpay", body=body, signature=signature, secret=SECRET)


def test_valid_signature_is_ingested(store):
    body = make_body()
    signature = sign(body, SECRET)
    result = verify_and_ingest(store=store, source="simulated", body=body, signature=signature, secret=SECRET)
    assert isinstance(result, IngestResult)
    assert result.is_duplicate is False
    assert result.event_id == "evt_1"


def test_invalid_signature_raises_before_touching_body(store):
    body = make_body()
    with pytest.raises(SignatureInvalid):
        verify_and_ingest(store=store, source="simulated", body=body, signature="0" * 64, secret=SECRET)
    assert store.count() == 0  # nothing was recorded — the bad delivery never reached parsing


def test_redelivery_of_the_same_event_id_is_flagged_duplicate_not_reprocessed(store):
    body = make_body(event_id="evt_redelivered")
    signature = sign(body, SECRET)

    first = verify_and_ingest(store=store, source="simulated", body=body, signature=signature, secret=SECRET)
    second = verify_and_ingest(store=store, source="simulated", body=body, signature=signature, secret=SECRET)

    assert first.is_duplicate is False
    assert second.is_duplicate is True
    assert store.count() == 1


def test_same_event_id_from_a_different_source_is_not_a_duplicate(store):
    body = make_body(event_id="evt_shared_id")
    signature = sign(body, SECRET)

    a = verify_and_ingest(store=store, source="razorpay", body=body, signature=signature, secret=SECRET)
    b = verify_and_ingest(store=store, source="simulated", body=body, signature=signature, secret=SECRET)

    assert a.is_duplicate is False
    assert b.is_duplicate is False
    assert store.count() == 2


def test_out_of_order_arrival_is_accepted_independently(store):
    """§9.3: order-tolerant state guards, not sequential assumptions — INGEST itself
    doesn't enforce ordering, it just records each event once."""
    captured_body = make_body(event="payment.captured", event_id="evt_captured")
    authorized_body = make_body(event="payment.authorized", event_id="evt_authorized")

    captured_first = verify_and_ingest(
        store=store, source="simulated", body=captured_body, signature=sign(captured_body, SECRET), secret=SECRET
    )
    authorized_second = verify_and_ingest(
        store=store, source="simulated", body=authorized_body, signature=sign(authorized_body, SECRET), secret=SECRET
    )
    assert captured_first.is_duplicate is False
    assert authorized_second.is_duplicate is False


def test_malformed_envelope_after_valid_signature_raises_malformed_not_silently_ignored(store):
    body = json.dumps({"not_an_envelope": True}, sort_keys=True).encode("utf-8")
    signature = sign(body, SECRET)
    with pytest.raises(MalformedWebhook):
        verify_and_ingest(store=store, source="simulated", body=body, signature=signature, secret=SECRET)
