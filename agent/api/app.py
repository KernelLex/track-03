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

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from agent.act.actions import ActionType
from agent.act.executor import OutboundActionStore
from agent.api.demo import router as demo_router
from agent.auditor.scheduler import start_auditor_scheduler
from agent.diagnose.taxonomy import UnknownFailureCode
from agent.ingest.listen import UnrecognizedWebhookEvent, facts_from_webhook
from agent.ingest.webhooks import EventStore, MalformedWebhook, SignatureInvalid, verify_and_ingest
from agent.ledger.store import Ledger
from agent.notify.telegram import TelegramChannel
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

    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    app.state.orchestrator_channel = TelegramChannel(telegram_token) if telegram_token else None
    app.state.orchestrator_contact_chat_id = os.environ.get("DEMO_CONTACT_TELEGRAM_CHAT_ID")

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
        if app.state.orchestrator_channel is not None:
            app.state.orchestrator_channel.close()


app = FastAPI(title="TrueCommit", lifespan=lifespan)

# Scoped to /demo/* only in effect (agent/api/demo.py refuses without the
# right secret regardless of origin) -- broad origins are acceptable here
# because the real protection is that endpoint's own secret + hardcoded
# recipient, not CORS. Every other route on this app has no browser-facing
# use case and isn't affected by this middleware being permissive.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["POST"], allow_headers=["*"],
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

    response: dict[str, object] = {"status": "ingested", "event_id": result.event_id, "facts": [f.name for f in facts]}
    if orchestration is not None:
        response["orchestration"] = orchestration
    return response


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
        channel_tag="telegram", ledger=state.orchestrator_ledger, outbound_store=state.orchestrator_store,
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
