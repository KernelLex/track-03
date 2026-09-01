"""A narrow, deliberately-limited endpoint that lets the published demo
Artifact trigger one real Telegram message or Twilio call -- not a general
"send to anyone" API.

Safety properties, all load-bearing for exposing this on a public tunnel:
  - The recipient is never taken from the request. It's read from
    DEMO_CONTACT_TELEGRAM_CHAT_ID / DEMO_CONTACT_PHONE_NUMBER on this
    server -- a caller can trigger a send *to the demo owner*, and to
    nobody else, no matter what they put in the request body.
  - Gated by a shared secret (DEMO_TRIGGER_SECRET) baked into the
    Artifact's JS. This is not a real auth boundary (visible to anyone who
    views the page source) -- it exists to stop the endpoint being found
    and hit by accident, not a determined attacker. The real protection is
    the two points above and below.
  - Rate-limited per channel (in-process, resets on restart -- proportionate
    for a demo, not a production rate limiter).
  - The proposed action still goes through the real check_bounds() gate
    before anything is sent, using a BoundsContext built from the request's
    own scenario -- refused the same way a real out-of-bounds action would
    be, not specially exempted.

Deliberately does not use agent.act.executor.execute_action(): that
function's idempotency (one claim per (debtor_id, invoice_id, action_type,
decision_seq)) exists to stop a *production* action from double-firing --
exactly wrong for a demo trigger meant to be clicked repeatably. The bounds
check runs for real; the ledger write does not, since this isn't a real
recovery action.

2026-09-01: the b2b scenario's Telegram send now includes a real Razorpay
payment link (`_create_real_payment_link`, same `RazorpayRail` the real
orchestration path uses -- test-mode account, so no real money moves) --
best-effort, the message still sends without one if link creation fails.
`check-reply` also sends a real, family-level follow-up back over the same
channel after diagnosing a reply (`_agent_reply_for`), turning this from a
one-shot send into an actual two-way exchange rather than a diagnosis that
only ever surfaces on the dashboard.
"""

from __future__ import annotations

import logging
import os
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent.auditor.extraction_log import ExtractionLog
from agent.bounds.context import ActionCtx, BoundsContext, ConfigCtx, DebtorCtx, DecisionCtx, InvoiceCtx, MandateCtx
from agent.bounds.engine import check_bounds
from agent.diagnose.extract import Family
from agent.diagnose.llm_extract import ExtractionFailed, extract_from_reply
from agent.notify.protocol import ChannelUnavailable
from agent.notify.telegram import TelegramChannel
from agent.notify.twilio_voice import TwilioVoiceChannel
from agent.rails.razorpay_rail import RazorpayRail
from agent.rails.types import LinkSpec

_log = logging.getLogger("trucommit.demo")

router = APIRouter(prefix="/demo", tags=["demo"])

MIN_SECONDS_BETWEEN_TRIGGERS = 20.0
_last_triggered_at: dict[str, float] = {}

# Set by a b2b trigger that successfully creates a real link, read back by
# check-reply's follow-up so a debtor asking for the link again (or just
# replying at all) gets the same real one, not a second freshly-created
# link on every reply -- one real Razorpay object per demo run, not per
# message. Best-effort: a b2b send still goes out with no link at all if
# Razorpay creation fails, rather than blocking the whole message on it.
_last_payment_link_url: str | None = None

# Diagnosing the same reply twice is harmless (cheap to repeat, same result
# either way) -- sending a real follow-up message twice for it is not.
# check-reply has no client-side guarantee against being asked about the
# same update_id again (a page reload resets the dashboard's own tracked
# position, and this endpoint has no other session concept) -- this is the
# actual guard against a duplicate real send, not the caller's good behavior.
_last_followed_up_update_id: int = 0

SCENARIOS: dict[str, dict[str, object]] = {
    "b2b": {
        "invoice_id": "INV-2201",
        "amount_paise": 42_500_00,
        "text_message": (
            "Hi, this is TrueCommit on behalf of Acme Textiles. Invoice INV-2201 for "
            "Rs 42,500 is now 22 days overdue. Reply here if anything about this invoice "
            "looks wrong."
        ),
        "text_voice": (
            "Hello, this is an automated call from True Commit, regarding invoice "
            "I N V dash 2 2 0 1, for 42,500 rupees, now 22 days overdue. Please check "
            "Telegram for a payment link. Thank you."
        ),
    },
    "subscription": {
        "invoice_id": "SUB-8834",
        "amount_paise": 999_00,
        "text_message": (
            "Hi, this is TrueCommit. Your subscription's next auto-debit of Rs 999 is "
            "coming up, and we've noticed your mandate may not clear it. Tap here to fix "
            "it before the debit is presented, so you don't get hit with a failed-payment "
            "notice."
        ),
        "text_voice": (
            "Hello, this is an automated call from True Commit, about your subscription. "
            "Your next auto debit of 999 rupees may fail. Please check Telegram to update "
            "your payment method before then. Thank you."
        ),
    },
    "escalation": {
        "invoice_id": "INV-5581",
        "amount_paise": 88_000_00,
        "text_message": (
            "Escalation: Invoice INV-5581 has a Rs 30,000 disputed portion (buyer says "
            "part of the order never arrived). check_bounds() refused every automated "
            "mandate/reminder action against it -- routing to human review now. "
            "Automated contact on this invoice is paused until you resolve it."
        ),
        "text_voice": (
            "Hello, this is an automated notification from True Commit. Invoice "
            "I N V dash 5 5 8 1 has a disputed amount and needs human review. Automated "
            "actions on this invoice are paused. Please check Telegram for details. "
            "Thank you."
        ),
    },
}


class DemoTriggerRequest(BaseModel):
    secret: str
    channel: str
    scenario: str


def _require_secret(secret: str) -> None:
    expected = os.environ.get("DEMO_TRIGGER_SECRET")
    if not expected or secret != expected:
        raise HTTPException(status_code=403, detail="invalid demo secret")


def _check_rate_limit(channel: str) -> None:
    now = time.monotonic()
    last = _last_triggered_at.get(channel, 0.0)
    if now - last < MIN_SECONDS_BETWEEN_TRIGGERS:
        wait = MIN_SECONDS_BETWEEN_TRIGGERS - (now - last)
        raise HTTPException(status_code=429, detail=f"triggered too recently -- wait {wait:.0f}s")
    _last_triggered_at[channel] = now


def _create_real_payment_link(scenario: dict[str, object]) -> str | None:
    """Best-effort: a real Razorpay payment link (test-mode account), the
    same rail (`RazorpayRail`) and object type the real orchestration path
    creates on a real payment.failed webhook -- not a second, fake-looking
    stand-in. Returns None (never raises) on any failure, so a missing link
    degrades the message rather than blocking the send entirely; the
    failure is still logged, not silently dropped."""
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        return None
    try:
        rail = RazorpayRail(key_id=key_id, key_secret=key_secret)
        link = rail.create_payment_link(LinkSpec(
            amount_paise=int(scenario["amount_paise"]),
            description=f"TrueCommit demo -- {scenario['invoice_id']}",
        ))
    except Exception:
        _log.warning("demo: real payment link creation failed", exc_info=True)
        return None
    global _last_payment_link_url
    _last_payment_link_url = link.short_url
    return link.short_url


def _agent_reply_for(family: Family) -> str:
    """A real follow-up sent back over the same channel after diagnosing a
    reply -- the piece that turns this from a one-shot "send and diagnose"
    into an actual two-way exchange. Family-level, not per-DiagnosisClass:
    29 classes' worth of bespoke replies would be a lot of surface for a
    demo follow-up to get subtly wrong, and the family-level distinction
    (instrument / blocker / liquidity / dispute) is already what the
    dashboard's own scripted Diagnose stage explains to a viewer."""
    if family == Family.A:
        return "Got it -- flagging that for repair on our end so it doesn't fail the same way again."
    if family == Family.B:
        return "Understood -- pausing automated contact on this invoice while that's sorted out on your end."
    if family == Family.D:
        return "This needs a person to review, not another automated message -- flagging your case now, and pausing automated contact on this invoice."
    # Family C: liquidity/willingness -- the one case where resending the
    # real link (if one exists from this run) is actually the right response.
    if _last_payment_link_url:
        return f"No problem -- here's the link again whenever you're ready: {_last_payment_link_url}"
    return "Understood -- no rush, it'll confirm itself once it's paid."


def _bounds_context_for(scenario: dict[str, object], channel: str) -> BoundsContext:
    return BoundsContext(
        debtor=DebtorCtx(id="demo_debtor", state="ENGAGED", touches_7d=0),
        mandate=MandateCtx(),
        action=ActionCtx(type="send_reminder", channel=channel, rail_tag="simulated"),
        decision=DecisionCtx(ev_paise=int(scenario["amount_paise"]) - 500),
        invoice=InvoiceCtx(id=str(scenario["invoice_id"]), recovery_attempts=1),
        config=ConfigCtx(),
    )


@router.post("/trigger")
def trigger_demo_contact(payload: DemoTriggerRequest) -> dict[str, object]:
    _require_secret(payload.secret)

    scenario = SCENARIOS.get(payload.scenario)
    if scenario is None:
        raise HTTPException(status_code=400, detail=f"unknown scenario {payload.scenario!r}")
    if payload.channel not in ("telegram", "ivr"):
        raise HTTPException(status_code=400, detail=f"unknown channel {payload.channel!r}")

    _check_rate_limit(payload.channel)

    bounds_result = check_bounds(_bounds_context_for(scenario, payload.channel))
    if not bounds_result.passed:
        raise HTTPException(
            status_code=422,
            detail=f"check_bounds() refused this demo action: "
                   f"{[v.rule_id for v in bounds_result.refusals]}",
        )

    if payload.channel == "telegram":
        chat_id = os.environ.get("DEMO_CONTACT_TELEGRAM_CHAT_ID")
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not chat_id or not token:
            raise HTTPException(status_code=503, detail="Telegram demo contact not configured on this server")
        channel_obj = TelegramChannel(token)
        to, text = chat_id, str(scenario["text_message"])
        if payload.scenario == "b2b":
            link_url = _create_real_payment_link(scenario)
            if link_url:
                text += f"\n\nPay now: {link_url}"
    else:
        phone = os.environ.get("DEMO_CONTACT_PHONE_NUMBER")
        sid = os.environ.get("TWILIO_ACCOUNT_SID")
        from_number = os.environ.get("TWILIO_FROM_NUMBER")
        api_key_sid = os.environ.get("TWILIO_API_KEY_SID")
        api_key_secret = os.environ.get("TWILIO_API_KEY_SECRET")
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        if not phone or not sid or not from_number or not (api_key_secret or auth_token):
            raise HTTPException(status_code=503, detail="Twilio demo contact not configured on this server")
        secret_value, username = (api_key_secret, api_key_sid) if api_key_secret else (auth_token, None)
        channel_obj = TwilioVoiceChannel(sid, secret_value, from_number, auth_username=username)
        to, text = phone, str(scenario["text_voice"])

    try:
        result = channel_obj.send(to=to, text=text)
    except ChannelUnavailable as exc:
        raise HTTPException(status_code=502, detail=f"channel unavailable: {exc}") from exc
    finally:
        channel_obj.close()

    return {
        "bounds_checks": [v.rule_id for v in bounds_result.verdicts if v.verdict == "PASS"],
        "channel": result.channel,
        "status": result.status,
        "external_ref": result.external_ref,
        "detail": result.detail,
    }


class CheckReplyRequest(BaseModel):
    secret: str
    after_update_id: int | None = None
    diagnose: bool = True


@router.post("/check-reply")
def check_reply(payload: CheckReplyRequest) -> dict[str, object]:
    """Polled by the dashboard after a live Telegram send, to make the demo
    genuinely reactive rather than fire-and-forget: did the debtor (the
    demo owner, replying on their own phone) reply yet, and if so, what
    does the real extractor (agent.diagnose.llm_extract) make of it.

    Cost is bounded by construction, not by a rate limit: `after_update_id`
    is round-tripped by the caller and passed straight to Telegram's own
    `offset` semantics, so a poll that finds nothing new costs nothing --
    only a genuinely new reply ever reaches extract_from_reply() (a real,
    budget-tracked call). No rate limit is applied here for that reason;
    /trigger's is about not spamming a real message/call, which doesn't
    apply to read-only polling.

    Only messages from the server-configured DEMO_CONTACT_TELEGRAM_CHAT_ID
    are ever considered -- a stranger messaging the bot during a live demo
    can never have their message surface as if it were the demo's own
    debtor replying.
    """
    _require_secret(payload.secret)

    chat_id = os.environ.get("DEMO_CONTACT_TELEGRAM_CHAT_ID")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not chat_id or not token:
        raise HTTPException(status_code=503, detail="Telegram demo contact not configured on this server")

    channel_obj = TelegramChannel(token)
    try:
        offset = (payload.after_update_id + 1) if payload.after_update_id is not None else None
        updates = channel_obj.get_updates(offset=offset)
    except ChannelUnavailable as exc:
        raise HTTPException(status_code=502, detail=f"channel unavailable: {exc}") from exc
    finally:
        channel_obj.close()

    matching = [
        u for u in updates
        if u.get("message") and "text" in u["message"] and str(u["message"]["chat"]["id"]) == str(chat_id)
    ]
    if not matching:
        return {"has_reply": False}

    latest = matching[-1]
    text = latest["message"]["text"]
    result: dict[str, object] = {
        "has_reply": True, "text": text, "update_id": latest["update_id"], "diagnosis": None, "agent_reply": None,
    }

    if payload.diagnose:
        try:
            log_path = os.environ.get("TRUECOMMIT_EXTRACTION_LOG", "extraction_log.db")
            with ExtractionLog(log_path) as extraction_log:
                extraction = extract_from_reply(text, purpose="demo_dashboard_live_reply", extraction_log=extraction_log)
            result["diagnosis"] = {
                "family": extraction.family.value,
                "class": extraction.class_.value,
                "confidence": extraction.confidence,
            }

            # The conversational half: a real message back over the same
            # channel, not just a diagnosis shown on a dashboard. Best-effort
            # -- a failed follow-up send still leaves the diagnosis itself
            # intact in the response rather than failing the whole poll.
            # Guarded against re-sending for an update_id already followed
            # up on (see _last_followed_up_update_id's comment) -- diagnosis
            # itself still re-runs every time, only the real send is skipped.
            global _last_followed_up_update_id
            if latest["update_id"] > _last_followed_up_update_id:
                reply_text = _agent_reply_for(extraction.family)
                reply_channel = TelegramChannel(token)
                try:
                    reply_channel.send(to=chat_id, text=reply_text)
                    result["agent_reply"] = reply_text
                    _last_followed_up_update_id = latest["update_id"]
                except ChannelUnavailable:
                    _log.warning("demo: follow-up reply send failed", exc_info=True)
                finally:
                    reply_channel.close()
        except ExtractionFailed as exc:
            result["diagnosis"] = {"error": str(exc)}

    return result
