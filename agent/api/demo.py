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
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent.bounds.context import ActionCtx, BoundsContext, ConfigCtx, DebtorCtx, DecisionCtx, InvoiceCtx, MandateCtx
from agent.bounds.engine import check_bounds
from agent.notify.protocol import ChannelUnavailable
from agent.notify.telegram import TelegramChannel
from agent.notify.twilio_voice import TwilioVoiceChannel

router = APIRouter(prefix="/demo", tags=["demo"])

MIN_SECONDS_BETWEEN_TRIGGERS = 20.0
_last_triggered_at: dict[str, float] = {}

SCENARIOS: dict[str, dict[str, object]] = {
    "b2b": {
        "invoice_id": "INV-2201",
        "amount_paise": 42_500_00,
        "text_message": (
            "Hi, this is TrueCommit on behalf of Acme Textiles. Invoice INV-2201 for "
            "Rs 42,500 is now 22 days overdue. Reply here if anything about this invoice "
            "looks wrong -- otherwise we'll send a one-tap payment link shortly."
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
    }
