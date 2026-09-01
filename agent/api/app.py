"""Minimal FastAPI app: the webhook receiver + a health check. DEVDOC_v6 §19.

Not the full dashboard DEVDOC_v6 envisions (Jinja templates, a human-queue
UI) — just enough that INGEST and LISTEN have a real HTTP endpoint to run
behind, since neither stage means anything without one. Wires directly into
the modules already built and tested in isolation
(`agent.ingest.webhooks.verify_and_ingest`, `agent.ingest.listen
.facts_from_webhook`) rather than reimplementing anything here.

Configuring a real Razorpay webhook against this endpoint needs a publicly
reachable URL and manual setup in the Razorpay dashboard — both outside
what this build can do unattended. See docs/LIMITATIONS.md.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from agent.act.actions import ActionType
from agent.act.executor import OutboundActionStore
from agent.ledger.recovery import NotCaptured, RecoveryLedger
from agent.api.demo import router as demo_router
from agent.auditor.scheduler import start_auditor_scheduler
from agent.diagnose.extract import DiagnosisClass, Family
from agent.diagnose.llm_extract import ExtractionFailed, extract_from_reply
from agent.diagnose.taxonomy import UnknownFailureCode
from agent.ingest.listen import UnrecognizedWebhookEvent, facts_from_webhook
from agent.ingest.webhooks import EventStore, MalformedWebhook, SignatureInvalid, verify_and_ingest
from agent.debtor.registry import DebtorRegistry
from agent.debtor.invoices import InvoiceStore
from agent.debtor.seed import seed_invoices, seed_registry
from agent.ledger.store import Ledger
from agent.money import to_rupees_display
from agent.notify.conversation import ConversationStore
from agent.notify.protocol import ChannelUnavailable
from agent.notify.telegram import TelegramChannel
from agent.notify.whatsapp import WhatsAppChannel, parse_incoming_messages, verify_webhook_challenge, verify_webhook_signature
from agent.orchestrate import UnmappedFailureCode, diagnose_from_failure_code, run_pipeline
from agent.rails.razorpay_rail import RazorpayRail
from agent.rails.simulated import SimulatedRail

_log = logging.getLogger("trucommit.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db_path = os.environ.get("TRUECOMMIT_EVENTS_DB", "events.db")
    app.state.event_store = EventStore(db_path)

    ledger_db_path = os.environ.get("TRUECOMMIT_LEDGER_DB")
    app.state.auditor_scheduler = start_auditor_scheduler(ledger_db_path) if ledger_db_path else None
    if app.state.auditor_scheduler is None:
        _log.warning(
            "TRUECOMMIT_LEDGER_DB not set -- the Auditor is not running. "
            "check_bounds() will still refuse bad actions; nothing is watching for a "
            "gate that silently stops being called (§11.7)."
        )

    # Auto-orchestration: DIAGNOSE -> DECIDE -> BOUNDS -> ACT, triggered by a
    # live webhook, nobody clicking (agent/orchestrate.py). Only runs when
    # TRUECOMMIT_LEDGER_DB is set -- without a real ledger there's nowhere
    # for Law 4 coordination to happen, so this stays off rather than run
    # against a throwaway in-memory one.
    app.state.orchestrator_ledger = Ledger(ledger_db_path) if ledger_db_path else None
    app.state.orchestrator_store = (
        OutboundActionStore(ledger_db_path.rsplit(".", 1)[0] + "_outbound.db") if ledger_db_path else None
    )
    # SETTLE's own store (§16, Law 7). Separate file from the hash-chained
    # ledger for the same reason the outbound claim table is: a different
    # table with a different uniqueness guarantee. Under Turso the path is
    # ignored and every store shares the one database (agent/db.py), so
    # this is a local-file distinction only.
    app.state.recovery_ledger = (
        RecoveryLedger(ledger_db_path.rsplit(".", 1)[0] + "_recovery.db") if ledger_db_path else None
    )
    # SimulatedRail by default for the auto-triggered path -- a real
    # RazorpayRail would create a real object in the merchant's account on
    # every single test webhook, which is the wrong default for a
    # dev/demo server. Set TRUECOMMIT_ORCHESTRATOR_RAIL=razorpay to use the
    # real one deliberately (needs RAZORPAY_KEY_ID/SECRET).
    if os.environ.get("TRUECOMMIT_ORCHESTRATOR_RAIL") == "razorpay":
        key_id, key_secret = os.environ.get("RAZORPAY_KEY_ID"), os.environ.get("RAZORPAY_KEY_SECRET")
        app.state.orchestrator_rail = RazorpayRail(key_id=key_id, key_secret=key_secret) if key_id and key_secret else None
    else:
        app.state.orchestrator_rail = SimulatedRail(webhook_secret=os.environ.get("TRUECOMMIT_WEBHOOK_SECRET_SIMULATED", "orchestrator"))

    # Channel selection: WhatsApp is the intended production channel once its
    # credentials exist (docs/WHATSAPP.md) -- preferred over Telegram when
    # both are configured, since Telegram was always a free stand-in for a
    # channel a debtor can't be cold-messaged on. Falls back to Telegram
    # (still real, still live) when WhatsApp isn't configured yet.
    whatsapp_phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    whatsapp_token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if whatsapp_phone_id and whatsapp_token:
        app.state.orchestrator_channel = WhatsAppChannel(whatsapp_phone_id, whatsapp_token)
        contact_phone = os.environ.get("DEMO_CONTACT_PHONE_NUMBER")
        app.state.orchestrator_contact_chat_id = contact_phone.lstrip("+") if contact_phone else None
        app.state.orchestrator_channel_tag = "whatsapp"
    elif telegram_token:
        app.state.orchestrator_channel = TelegramChannel(telegram_token)
        app.state.orchestrator_contact_chat_id = os.environ.get("DEMO_CONTACT_TELEGRAM_CHAT_ID")
        app.state.orchestrator_channel_tag = "telegram"
    else:
        app.state.orchestrator_channel = None
        app.state.orchestrator_contact_chat_id = None
        app.state.orchestrator_channel_tag = None
    # Recorded rather than hardcoded at the call site, where it was always
    # "telegram" regardless of which channel was actually selected above.
    # That made the bounds gate reason about a channel the send was not
    # going over: TRAI_DND checked the wrong channel's opt-out, and
    # WHATSAPP_SESSION_WINDOW -- which only fires on channel == 'whatsapp'
    # -- could never fire on the one automated path able to send there.
    # Latent while WhatsApp is unconfigured, and wrong either way.

    # Seed the debtor register: four declared histories spanning the score
    # bands, plus the live demo contact with no history at all. Idempotent,
    # and it never touches a real debtor's earned score (agent/debtor/seed.py).
    try:
        registry = DebtorRegistry(os.environ.get("TRUECOMMIT_DEBTORS_DB", "debtors.db"))
        try:
            seed_registry(registry)
        finally:
            registry.close()
        store = InvoiceStore(os.environ.get("TRUECOMMIT_DEBTORS_DB", "debtors.db"))
        try:
            seed_invoices(store)
        finally:
            store.close()
    except Exception:
        _log.warning("could not seed the debtor register", exc_info=True)

    try:
        yield
    finally:
        app.state.event_store.close()
        if app.state.auditor_scheduler is not None:
            app.state.auditor_scheduler.shutdown(wait=False)
        if app.state.orchestrator_ledger is not None:
            app.state.orchestrator_ledger.close()
        if app.state.orchestrator_store is not None:
            app.state.orchestrator_store.close()
        if app.state.recovery_ledger is not None:
            app.state.recovery_ledger.close()
        if app.state.orchestrator_channel is not None:
            app.state.orchestrator_channel.close()


app = FastAPI(title="TrueCommit", lifespan=lifespan)

# Scoped to /demo/* only in effect (agent/api/demo.py refuses without the
# right secret regardless of origin) -- broad origins are acceptable here
# because the real protection is that endpoint's own secret + hardcoded
# recipient, not CORS. Every other route on this app has no browser-facing
# use case and isn't affected by this middleware being permissive.
#
# GET is allowed for /demo/timeline, which the dashboard reads directly
# rather than through the site's serverless proxy. It carries no secret --
# it is a read of the demo's own scripted invoice and the demo owner's own
# replies to their own bot -- so there is nothing for a proxy to hide, and
# routing a public read through one would only add a hop and a cold start.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"],
)

app.include_router(demo_router)


def _webhook_secret_for(source: str) -> str:
    """One shared secret per source, from the environment. Raises rather than
    accepting an unverifiable webhook when unconfigured."""
    env_var = f"TRUECOMMIT_WEBHOOK_SECRET_{source.upper()}"
    secret = os.environ.get(env_var)
    if not secret:
        raise HTTPException(status_code=500, detail=f"no webhook secret configured for source={source!r} (set {env_var})")
    return secret


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


WHATSAPP_BUTTON_DIAGNOSIS: dict[str, tuple[Family, DiagnosisClass]] = {
    # A fixed, small mapping from a tapped reply-button id to a Path A
    # diagnosis -- no model involved, same reasoning as
    # agent.orchestrate.diagnose_from_failure_code: the debtor chose from a
    # known, finite set of options, so there's nothing to extract. These
    # three ids are the ones agent.notify.whatsapp.WhatsAppChannel
    # .send_interactive_buttons is meant to be called with; a real deployment
    # would keep the ids in sync with whatever buttons a live template
    # actually ships (Meta requires template approval for buttons that open
    # a conversation from cold -- these are for the in-window follow-up
    # case only).
    "btn_already_paid": (Family.B, DiagnosisClass.ALREADY_PAID_UNRECONCILED),
    "btn_dispute": (Family.D, DiagnosisClass.AMOUNT),
    "btn_need_time": (Family.C, DiagnosisClass.STALLING),
}


# Registered before the generic /webhooks/{source} route below -- Starlette
# matches path routes in registration order for a given method, and these
# two static paths must win over that route's {source} wildcard, or a
# POST to /webhooks/whatsapp would be swallowed by receive_webhook(source=
# "whatsapp", ...) instead of ever reaching Meta's own signature scheme.
@app.get("/webhooks/whatsapp")
def verify_whatsapp_webhook(request: Request) -> PlainTextResponse:
    """Meta's one-time GET handshake when this URL is registered in the App
    Dashboard (agent/notify/whatsapp.py's verify_webhook_challenge)."""
    expected = os.environ.get("WHATSAPP_WEBHOOK_VERIFY_TOKEN")
    if not expected:
        raise HTTPException(status_code=500, detail="WHATSAPP_WEBHOOK_VERIFY_TOKEN not configured")
    params = request.query_params
    challenge = verify_webhook_challenge(
        mode=params.get("hub.mode"), token=params.get("hub.verify_token"),
        challenge=params.get("hub.challenge"), expected_token=expected,
    )
    if challenge is None:
        raise HTTPException(status_code=403, detail="webhook verification failed")
    return PlainTextResponse(challenge)


@app.post("/webhooks/whatsapp")
async def receive_whatsapp_webhook(request: Request) -> dict[str, object]:
    """Real inbound WhatsApp messages, signature-verified with Meta's own
    X-Hub-Signature-256 scheme (a different credential -- the Meta App's
    secret -- from the WHATSAPP_ACCESS_TOKEN used to send).

    Diagnoses each message immediately (Path A for a button tap via
    WHATSAPP_BUTTON_DIAGNOSIS, Path B via the real extract_from_reply() for
    free text) and returns the diagnosis -- but deliberately does NOT call
    run_pipeline() the way the Razorpay payment.failed path does. That path
    can derive a debtor_id/invoice_id from the payment itself; a WhatsApp
    message only carries a wa_id (a phone number), which this build has no
    merchant AR system to resolve to a real invoice. Wiring an inbound reply
    into DECIDE/BOUNDS/ACT needs that lookup first -- a real next step, not
    done here (see docs/WHATSAPP.md).

    Deduplicated against redelivery using the same EventStore every
    Razorpay webhook dedupes through, keyed by each message's own wamid --
    without this, a Meta retry would double-bill a real Claude call for the
    same free-text reply (agent/spend.py's $20 ceiling is per real reply,
    not per delivery attempt).
    """
    body = await request.body()
    signature = request.headers.get("x-hub-signature-256")
    app_secret = os.environ.get("WHATSAPP_APP_SECRET")
    if not app_secret:
        raise HTTPException(status_code=500, detail="WHATSAPP_APP_SECRET not configured")
    if not verify_webhook_signature(body, signature, app_secret):
        raise HTTPException(status_code=400, detail="invalid webhook signature")

    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"malformed JSON: {exc}") from exc

    messages = parse_incoming_messages(payload)
    results: list[dict[str, object]] = []
    for msg in messages:
        if not request.app.state.event_store.record("whatsapp", msg.message_id, msg.type):
            results.append({"from": msg.from_wa_id, "duplicate": True})
            continue

        if msg.is_structured_reply:
            mapping = WHATSAPP_BUTTON_DIAGNOSIS.get(msg.button_id or "")
            diagnosis = (
                {"family": mapping[0].value, "class": mapping[1].value, "confidence": 1.0}
                if mapping is not None else None
            )
            results.append({"from": msg.from_wa_id, "type": "interactive", "button_id": msg.button_id, "diagnosis": diagnosis})
        else:
            try:
                extraction = extract_from_reply(msg.text or "", purpose="whatsapp_inbound_reply")
                diagnosis = {
                    "family": extraction.family.value, "class": extraction.class_.value,
                    "confidence": extraction.confidence,
                }
            except ExtractionFailed as exc:
                diagnosis = {"error": str(exc)}
            results.append({"from": msg.from_wa_id, "type": "text", "text": msg.text, "diagnosis": diagnosis})

    return {"status": "processed", "messages": results}


@app.post("/webhooks/{source}")
async def receive_webhook(source: str, request: Request) -> dict[str, object]:
    body = await request.body()
    signature = request.headers.get("x-razorpay-signature") or request.headers.get("x-webhook-signature")
    if not signature:
        raise HTTPException(status_code=400, detail="missing signature header")

    secret = _webhook_secret_for(source)

    try:
        result = verify_and_ingest(
            store=request.app.state.event_store, source=source, body=body, signature=signature, secret=secret,
            event_id_header=request.headers.get("x-razorpay-event-id"),
        )
    except SignatureInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MalformedWebhook as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if result.is_duplicate:
        return {"status": "duplicate", "event_id": result.event_id}

    try:
        facts = facts_from_webhook(result)
    except UnrecognizedWebhookEvent:
        # Signature valid, redelivery-deduped -- LISTEN just has no extraction
        # rule for this event_type yet. 200: the sender shouldn't retry a
        # delivery this endpoint understood and recorded just fine.
        return {"status": "ingested_unrecognized_event", "event_id": result.event_id, "event_type": result.event_type}

    orchestration = _maybe_orchestrate(request.app.state, facts)
    settlement = _maybe_settle(request.app.state, facts)
    # Told after settling, so a "payment received" message can never go out
    # for a capture the ledger refused to attribute.
    outcome_notice = _notify_payment_outcome(request.app.state, facts)

    response: dict[str, object] = {"status": "ingested", "event_id": result.event_id, "facts": [f.name for f in facts]}
    if orchestration is not None:
        response["orchestration"] = orchestration
    if settlement is not None:
        response["settlement"] = settlement
    if outcome_notice is not None:
        response["debtor_notified"] = outcome_notice
    return response


def _maybe_settle(state, facts: list) -> dict[str, object] | None:
    """SETTLE (§16, Law 7): a rail-confirmed capture becomes recovered money,
    exactly once.

    This is the half of the pipeline that had never run against a real
    payment. DIAGNOSE->DECIDE->BOUNDS->ACT was wired to a live webhook
    weeks before this was; until now a real `payment.captured` arriving here
    was ingested, turned into facts, and then dropped, because
    `_maybe_orchestrate` only ever looked for a failure code. That gap is
    why `docs/RESULTS.md` could only call recovery "a modelling convention
    for my harness" -- the number came from the simulation harness, never
    from the rail.

    Three properties are load-bearing and none of them are new code -- they
    are `RecoveryLedger.attribute()`'s, which this only calls:
      - **Only 'captured' counts.** A payment in any other status raises
        NotCaptured rather than being counted; an authorization is not a
        recovery (§16: "not authorized, not created").
      - **Counted once.** `UNIQUE(payment_id)` in the database decides that,
        not application logic -- so a redelivery that somehow passed INGEST's
        own dedup still can't double-count. Returns None, which is a
        duplicate, not an error.
      - **Tagged with the rail that produced it** (Law 6): `rail_tag`
        "razorpay" here, never "simulated".

    ids come from the payment's own `notes` when the merchant set them
    (agent/ingest/listen.py extracts those), then the real `invoice_id` a
    payment against an invoice carries, and only then a derived placeholder
    -- the honest fallback for a build with no AR system behind it.
    """
    if state.recovery_ledger is None:
        return None  # no TRUECOMMIT_LEDGER_DB configured -- see the lifespan warning

    fact_map = {f.name: f.value for f in facts}
    if fact_map.get("payment_status") != "captured":
        return None  # nothing to settle -- the only status that counts (§16)

    payment_id = fact_map.get("payment_id")
    amount_paise = fact_map.get("payment_amount_paise")
    if not payment_id or not amount_paise:
        _log.warning("settle: captured payment with no id/amount to attribute -- skipping")
        return None

    invoice_id = fact_map.get("invoice_id") or f"invoice_{payment_id}"
    debtor_id = fact_map.get("debtor_id") or f"debtor_{payment_id}"

    try:
        entry = state.recovery_ledger.attribute(
            payment_id=payment_id, payment_status="captured",
            invoice_id=invoice_id, debtor_id=debtor_id,
            amount_paise=int(amount_paise), rail_tag="razorpay",
        )
    except NotCaptured as exc:  # pragma: no cover -- guarded above, kept so it can never pass silently
        _log.warning("settle: refused to attribute: %s", exc)
        return None

    if entry is None:
        return {"attributed": False, "reason": "already_attributed", "payment_id": payment_id}

    _log.info("settle: attributed %s paise from %s to invoice %s", entry.amount_paise, payment_id, invoice_id)
    return {
        "attributed": True,
        "payment_id": entry.payment_id,
        "invoice_id": entry.invoice_id,
        "debtor_id": entry.debtor_id,
        "amount_paise": entry.amount_paise,
        "rail_tag": entry.rail_tag,
        "recorded_at": entry.recorded_at,
    }


def _maybe_orchestrate(state, facts: list) -> dict[str, object] | None:
    """The "nobody clicking" path: a payment.failed webhook that just landed
    triggers DIAGNOSE -> DECIDE -> BOUNDS -> ACT immediately, for real --
    not a second copy of the pipeline, a call into agent.orchestrate, the
    same module a batch run or a scheduled sweep would call too.

    debtor_id/invoice_id are derived from the payment's own id as an honest
    stand-in: this build has no merchant AR system connected to look up the
    real ones from. A real deployment would read them from the payment's
    `notes` (Razorpay supports arbitrary merchant-supplied metadata there)
    instead of this placeholder scheme.
    """
    if state.orchestrator_ledger is None:
        return None  # no TRUECOMMIT_LEDGER_DB configured -- see the lifespan warning

    fact_map = {f.name: f.value for f in facts}
    code = fact_map.get("payment_failure_code")
    if code is None:
        return None  # nothing Path A can diagnose from this event

    payment_id = fact_map.get("payment_id", "unknown")
    amount_paise = fact_map.get("payment_amount_paise", 0)
    if not amount_paise:
        _log.warning("orchestrator: payment_failure_code present but no payment_amount_paise -- skipping (payment_id=%s)", payment_id)
        return None

    try:
        diagnosis = diagnose_from_failure_code(code)
    except (UnknownFailureCode, UnmappedFailureCode) as exc:
        _log.warning("orchestrator: could not diagnose failure code %r: %s", code, exc)
        return None

    # Deliberately NOT passing disposition_for_code(code) here: RETRYABLE
    # would select RETRY_CHARGE, which calls rail.present_debit(mandate_id,
    # ...) -- a real, existing mandate. facts_from_webhook() extracts no
    # mandate_id fact for a plain payment.failed event, so there is no real
    # mandate to retry against. Passing disposition=None always selects
    # CREATE_PAYMENT_LINK (see select_action_for_diagnosis), the correct
    # default absent mandate context -- RETRY_CHARGE stays real and correct
    # for a caller (a mandate-health sweep, say) that actually has one.

    debtor_id, invoice_id = f"debtor_{payment_id}", f"invoice_{payment_id}"
    to = state.orchestrator_contact_chat_id
    message_text = (
        f"Hi, this is TrueCommit. Payment {payment_id} for this invoice didn't go through "
        f"({code}). We'll follow up with the right next step shortly."
        if to else None
    )

    result = run_pipeline(
        debtor_id=debtor_id, invoice_id=invoice_id, amount_paise=amount_paise, diagnosis=diagnosis,
        channel_tag=getattr(state, "orchestrator_channel_tag", None) or "telegram",
        ledger=state.orchestrator_ledger, outbound_store=state.orchestrator_store,
        rail=state.orchestrator_rail, channel=state.orchestrator_channel, to=to, message_text=message_text,
    )
    _log.info(
        "orchestrator: debtor=%s action=%s bounds_passed=%s ev_paise=%s",
        debtor_id, result.action_type.value, result.bounds_passed, result.ev_paise,
    )

    notified = _notify_link_if_created(state, result, amount_paise=amount_paise, to=to)

    return {
        "debtor_id": debtor_id, "diagnosis": {"family": diagnosis.family.value, "class": diagnosis.class_.value},
        "action_type": result.action_type.value, "bounds_passed": result.bounds_passed,
        "refusal_reasons": result.refusal_reasons,
        "external_ref": result.action_outcome.external_ref if result.action_outcome else None,
        "notified": notified,
    }


def _notify_link_if_created(state, result, *, amount_paise: int, to: str | None) -> bool:
    """A payment link nobody's told about doesn't recover anything --
    CREATE_PAYMENT_LINK/REISSUE_ARTIFACT don't dispatch through a channel
    themselves (they're rail actions, not MESSAGE_ONLY_ACTIONS), so ACT
    creating the link isn't the end of the story the way it is for
    SEND_REMINDER/ESCALATE_HUMAN. This is a second, real send -- not a
    second orchestration run -- using the real short_url the rail actually
    returned, only when there's a real channel and a real contact to send
    it to."""
    if not result.bounds_passed or result.action_outcome is None or state.orchestrator_channel is None or not to:
        return False
    if result.action_type not in (ActionType.CREATE_PAYMENT_LINK, ActionType.REISSUE_ARTIFACT):
        return False
    short_url = result.action_outcome.detail.get("short_url")
    if not short_url:
        return False

    text = f"Hi, this is TrueCommit. Here's a payment link for Rs {amount_paise / 100:,.2f}: {short_url}"
    send_result = state.orchestrator_channel.send(to=to, text=text)
    return send_result.status == "sent"


# --------------------------------------------------------------------------
# Telling the debtor what happened to their payment.
# --------------------------------------------------------------------------

def _notify_payment_outcome(state, facts: list) -> dict[str, object] | None:
    """A capture or a failure is told to the debtor, on the channel they
    have been talking on.

    The gap this closes: the whole conversation was about getting someone to
    pay, and the moment they did, the system went silent. A debtor who
    authorizes a mandate and then hears nothing has no way to know it
    worked -- and one whose payment *failed* is the person most in need of
    being told, since they believe they have paid and will not act again
    until told otherwise.

    A capture also settles the promise it answers, which is what moves the
    debtor's score. That is deliberately driven from the rail's own event
    rather than from anything said in the conversation: Law 7's standard is
    a confirmed capture, and a score built on anything softer would be a
    score built on how convincing someone sounded.
    """
    fact_map = {f.name: f.value for f in facts}
    status = fact_map.get("payment_status")
    failure_code = fact_map.get("payment_failure_code")
    if status != "captured" and not failure_code:
        return None  # not a payment outcome

    chat_id = os.environ.get("DEMO_CONTACT_TELEGRAM_CHAT_ID")
    if not chat_id:
        return None

    amount_paise = int(fact_map.get("payment_amount_paise") or 0)
    payment_id = str(fact_map.get("payment_id") or "")
    invoice_id = fact_map.get("invoice_id")

    # Settling and recording happen first, and unconditionally. Whether a
    # payment counts toward a debtor's record is a fact about the payment;
    # it must not depend on whether a messaging channel happens to be
    # configured. Getting this backwards meant a deployment with no
    # Telegram token silently stopped scoring altogether.
    settled = False
    if status == "captured":
        settled = _settle_promise_for(str(chat_id), payment_id=payment_id, invoice_id=invoice_id)

    if status == "captured":
        amount = to_rupees_display(amount_paise) if amount_paise else "the payment"
        text = (
            f"Payment received -- {amount} against {invoice_id or 'your invoice'} has cleared "
            f"({payment_id}). Nothing further is owed on this invoice and automated follow-up "
            "has stopped."
        )
        if settled:
            text += " Thanks for paying on the date you named -- that's on your record."
    else:
        # Deliberately not a diagnosis or a demand. DIAGNOSE -> DECIDE ->
        # BOUNDS -> ACT is already running on this same webhook and owns
        # what happens next; this message exists only so the debtor is not
        # left believing a failed payment succeeded.
        text = (
            f"That payment didn't go through ({failure_code}). Nothing has been taken from your "
            "account. We're looking at why, and you'll get a working way to pay shortly -- "
            "no need to try again in the meantime."
        )

    kind = "payment_captured" if status == "captured" else "payment_failed"
    detail = {"payment_id": payment_id, "amount_paise": amount_paise,
              "invoice_id": invoice_id, "failure_code": failure_code,
              "promise_settled": settled, "text": text}

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        _record_payment_event(str(chat_id), kind=kind, detail={**detail, "notified": False})
        return {"notified": False, "reason": "no_channel_configured", "promise_settled": settled}

    try:
        channel = TelegramChannel(token)
        try:
            result = channel.send(to=str(chat_id), text=text)
        finally:
            channel.close()
    except ChannelUnavailable:
        _log.warning("payment outcome: could not tell the debtor", exc_info=True)
        _record_payment_event(str(chat_id), kind=kind, detail={**detail, "notified": False})
        return {"notified": False, "reason": "channel_unavailable", "promise_settled": settled}

    _record_payment_event(str(chat_id), kind=kind, detail=detail)
    return {"notified": True, "status": result.status, "promise_settled": settled}


def _settle_promise_for(channel_ref: str, *, payment_id: str, invoice_id: str | None) -> bool:
    """Keep the debtor's oldest open promise, once, on a real capture."""
    if not payment_id:
        return False
    try:
        registry = DebtorRegistry(os.environ.get("TRUECOMMIT_DEBTORS_DB", "debtors.db"))
        try:
            debtor = registry.by_channel_ref(channel_ref)
            if debtor is None:
                return False
            return registry.settle_promise(debtor.id, payment_id=payment_id, invoice_id=invoice_id)
        finally:
            registry.close()
    except Exception:
        _log.warning("payment outcome: could not settle the promise", exc_info=True)
        return False


def _record_payment_event(conversation_id: str, *, kind: str, detail: dict) -> None:
    """Onto the same timeline the dashboard reads, so a payment appears in
    the case file next to the conversation that produced it."""
    try:
        store = ConversationStore(os.environ.get("TRUECOMMIT_CONVERSATION_DB", "conversation.db"))
        try:
            store.record_event(conversation_id, kind=kind, channel="telegram", detail=detail)
            store.record_turn(conversation_id, direction="outbound", text=detail.get("text", ""))
        finally:
            store.close()
    except Exception:
        _log.warning("payment outcome: could not record the event", exc_info=True)
