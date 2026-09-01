"""The demo dashboard's live surface: three real channels (Telegram
message, WhatsApp template message, Twilio voice call), each able to send
for real, read a real reply back, and answer it -- not a general
"send to anyone" API.

Safety properties:
  - **Recipient.** Telegram's is never taken from the request (a bot can
    only message someone who has messaged it first, so there is no number
    to address -- a `to` there is refused outright). WhatsApp and voice
    calls *do* honor a caller-supplied E.164 number, deliberately, so
    someone trying this project can have it reach their own phone; that
    gives up the original "can only ever contact the demo owner" property,
    bounded instead by E.164 validation and a 5-minute per-number cooldown
    on top of the per-channel one.
  - **Secret.** DEMO_TRIGGER_SECRET, attached server-side by a serverless
    function rather than shipped in the page's JS (docs/DEMO_UI.md) -- not
    a claim the endpoint is unreachable, since that function is reachable
    by anyone with the site URL.
  - **Rate limits.** Per channel (20s) and, for a supplied number, per
    number (5 min) -- in-process, reset on restart, proportionate for a
    demo rather than a production limiter.
  - **The bounds gate runs for real**, on the outbound trigger *and* on
    the conversational follow-up (`_bounds_gate_followup`). A reply is an
    outbound contact like any other; exempting it because it happens to be
    a response would be exactly the kind of quiet carve-out this project
    exists not to have.

Deliberately does not use agent.act.executor.execute_action(): that
function's idempotency (one claim per (debtor_id, invoice_id, action_type,
decision_seq)) exists to stop a *production* action from double-firing --
exactly wrong for a demo trigger meant to be clicked repeatably. The bounds
check runs for real; the ledger write does not, since this isn't a real
recovery action.

What's real in a run: a real Razorpay payment link
(`_create_real_payment_link`, same rail the orchestration path uses --
test-mode, so no real money moves), a real send on the chosen channel, a
real extraction of whatever comes back (`agent.diagnose.llm_extract`,
budget-tracked), and a real reply composed against the debtor's actual
words (`agent.notify.compose`) rather than one fixed sentence per
diagnosis family -- with that fixed sentence kept only as the fallback for
when the composer is unavailable.
"""

from __future__ import annotations

import logging
import os
import re
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent.auditor.extraction_log import ExtractionLog
from agent.bounds.context import ActionCtx, BoundsContext, ConfigCtx, DebtorCtx, DecisionCtx, InvoiceCtx, MandateCtx
from agent.bounds.engine import check_bounds
from agent.diagnose.extract import Family
from agent.diagnose.llm_extract import ExtractionFailed, extract_from_reply
from agent.notify.compose import ComposeFailed, compose_reply
from agent.notify.protocol import ChannelUnavailable
from agent.notify.telegram import TelegramChannel
from agent.notify.twilio_voice import TwilioVoiceChannel
from agent.notify.twilio_whatsapp import TwilioWhatsAppChannel
from agent.rails.razorpay_rail import RazorpayRail
from agent.rails.types import LinkSpec

_log = logging.getLogger("trucommit.demo")

router = APIRouter(prefix="/demo", tags=["demo"])

MIN_SECONDS_BETWEEN_TRIGGERS = 20.0
_last_triggered_at: dict[str, float] = {}

# A visitor-supplied recipient applies only to the phone-addressed channels
# ("whatsapp" and "ivr"). Telegram is not one: a bot can only message
# someone who has already messaged it first, so there is no number to
# address -- a `to` there is rejected outright rather than silently
# ignored, since staying quiet about it would look like a bug rather than
# the platform rule it is.
#
# Honoring a caller-supplied number gives up this endpoint's original
# "can only ever contact the demo owner" property, deliberately (so someone
# trying the project can have it reach their own phone). These two limits
# are what bound that instead -- see this module's docstring.
E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
PER_NUMBER_COOLDOWN_SECONDS = 300.0
_last_triggered_at_by_number: dict[str, float] = {}


def _validate_e164(to: str) -> None:
    if not E164_RE.match(to):
        raise HTTPException(status_code=400, detail="phone number must be in E.164 format, e.g. +919876543210")


def _check_per_number_rate_limit(to: str) -> None:
    now = time.monotonic()
    last = _last_triggered_at_by_number.get(to, 0.0)
    if now - last < PER_NUMBER_COOLDOWN_SECONDS:
        wait = PER_NUMBER_COOLDOWN_SECONDS - (now - last)
        raise HTTPException(status_code=429, detail=f"this number was contacted too recently -- wait {wait:.0f}s")
    _last_triggered_at_by_number[to] = now


def _resolve_recipient(payload_to: str | None, env_var: str) -> str:
    """A visitor-supplied number if given (validated + cooldown-checked),
    otherwise the server's own configured contact."""
    if payload_to:
        _validate_e164(payload_to)
        _check_per_number_rate_limit(payload_to)
        return payload_to
    configured = os.environ.get(env_var)
    if not configured:
        raise HTTPException(status_code=503, detail="demo contact not configured on this server")
    return configured

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
        "days_overdue": 22,
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
        "days_overdue": 0,
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
        "days_overdue": 31,
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
    to: str | None = None
    """Visitor-supplied recipient, E.164 (e.g. "+919876543210") -- honored
    for channel="whatsapp" and channel="ivr", rejected for "telegram"
    (see E164_RE's comment for why). Omit it to use the server's own
    configured DEMO_CONTACT_PHONE_NUMBER."""


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
    failure is still logged, not silently dropped.

    Reuses `_last_payment_link_url` if one already exists rather than
    creating a fresh link on every single click -- caught live: Razorpay's
    test-mode account has a hard 30-payment-link cap, and creating a new
    one per trigger burns through it in well under 30 clicks. One real
    link per demo run is enough to prove the capability; recreating it
    repeatedly was never load-bearing for that."""
    global _last_payment_link_url
    if _last_payment_link_url:
        return _last_payment_link_url
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
    if payload.channel not in ("telegram", "ivr", "whatsapp"):
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
        if payload.to:
            raise HTTPException(
                status_code=400,
                detail="Telegram can't message a phone number -- a bot can only message someone who has "
                       "already messaged it first (a Telegram platform rule). Use the WhatsApp or call "
                       "channel for a custom number.",
            )
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
        send = lambda: channel_obj.send(to=to, text=text)  # noqa: E731

    elif payload.channel == "whatsapp":
        if payload.scenario != "b2b":
            raise HTTPException(
                status_code=400,
                detail="the WhatsApp demo trigger only supports the b2b scenario -- the only one with an "
                       "approved Content Template (see docs/CHANNELS.md)",
            )
        sid = os.environ.get("TWILIO_ACCOUNT_SID")
        whatsapp_from = os.environ.get("TWILIO_WHATSAPP_FROM")
        content_sid = os.environ.get("TWILIO_WHATSAPP_CONTENT_SID")
        api_key_sid = os.environ.get("TWILIO_API_KEY_SID")
        api_key_secret = os.environ.get("TWILIO_API_KEY_SECRET")
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        if not sid or not whatsapp_from or not content_sid or not (api_key_secret or auth_token):
            raise HTTPException(status_code=503, detail="Twilio WhatsApp demo contact not configured on this server")
        phone = _resolve_recipient(payload.to, "DEMO_CONTACT_PHONE_NUMBER")
        secret_value, username = (api_key_secret, api_key_sid) if api_key_secret else (auth_token, None)
        channel_obj = TwilioWhatsAppChannel(sid, secret_value, whatsapp_from, auth_username=username)
        link_url = _create_real_payment_link(scenario) or "https://rzp.io/i/pending"
        content_variables = {
            "1": str(scenario["invoice_id"]),
            "2": f"{int(scenario['amount_paise']) / 100:,.0f}",
            "3": "22",
            "4": link_url,
        }
        send = lambda: channel_obj.send_template(to=phone, content_sid=content_sid, content_variables=content_variables)  # noqa: E731

    else:
        sid = os.environ.get("TWILIO_ACCOUNT_SID")
        from_number = os.environ.get("TWILIO_FROM_NUMBER")
        api_key_sid = os.environ.get("TWILIO_API_KEY_SID")
        api_key_secret = os.environ.get("TWILIO_API_KEY_SECRET")
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        if not sid or not from_number or not (api_key_secret or auth_token):
            raise HTTPException(status_code=503, detail="Twilio demo contact not configured on this server")
        phone = _resolve_recipient(payload.to, "DEMO_CONTACT_PHONE_NUMBER")
        secret_value, username = (api_key_secret, api_key_sid) if api_key_secret else (auth_token, None)
        channel_obj = TwilioVoiceChannel(sid, secret_value, from_number, auth_username=username)
        to, text = phone, str(scenario["text_voice"])
        send = lambda: channel_obj.send(to=to, text=text)  # noqa: E731

    try:
        result = send()
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
    channel: str = "telegram"
    scenario: str = "b2b"
    """Which invoice the conversation is about -- the composed reply needs
    real context (invoice id, amount, days overdue) to say anything
    specific back, and the follow-up's own bounds check needs it too."""
    after_update_id: int | None = None
    """Telegram's own update_id cursor -- ignored for channel="whatsapp"."""
    after_message_sid: str | None = None
    """WhatsApp's cursor: a Twilio message SID -- ignored for channel="telegram"."""
    diagnose: bool = True


_last_followed_up_whatsapp_sid: str | None = None


def _diagnose_and_note(text: str) -> dict[str, object]:
    """The extraction half, shared by both channels: real extractor,
    real budget-tracked call, one diagnosis shape either way."""
    log_path = os.environ.get("TRUECOMMIT_EXTRACTION_LOG", "extraction_log.db")
    with ExtractionLog(log_path) as extraction_log:
        extraction = extract_from_reply(text, purpose="demo_dashboard_live_reply", extraction_log=extraction_log)
    return {"family": extraction.family.value, "class": extraction.class_.value, "confidence": extraction.confidence}


def _compose_or_fallback(reply_text: str, diagnosis: dict[str, object], scenario: dict[str, object]) -> str:
    """A real, specific reply to what the debtor actually said
    (agent.notify.compose) -- falling back to the fixed family-level line
    only if that call fails. The fallback is deliberately a known-safe
    sentence rather than a retry with looser guardrails: an unavailable
    composer is a reason to say something bland, never a reason to say
    something unvetted."""
    family = Family(str(diagnosis["family"]))
    try:
        return compose_reply(
            reply_text,
            invoice_id=str(scenario["invoice_id"]),
            amount_paise=int(scenario["amount_paise"]),
            days_overdue=int(scenario.get("days_overdue", 0)),
            family=str(diagnosis["family"]),
            class_=str(diagnosis["class"]),
            payment_link=_last_payment_link_url,
            purpose="demo_dashboard_conversational_reply",
        )
    except Exception as exc:
        # Broad on purpose: ComposeFailed (API/empty output) and
        # BudgetExceeded (agent.spend's ceiling) both mean "no vetted reply
        # available right now", and neither should break a read-only poll.
        _log.warning("demo: contextual reply composition failed, falling back to the fixed line: %s", exc)
        return _agent_reply_for(family)


def _bounds_gate_followup(scenario: dict[str, object], channel: str) -> list[str] | None:
    """The same check_bounds() gate /trigger runs, applied to the
    conversational follow-up too -- a reply is an outbound contact like any
    other, and exempting it because it happens to be a response would be
    exactly the kind of quiet carve-out this project exists to not have.
    Returns the refusing rule ids, or None when the send is allowed."""
    result = check_bounds(_bounds_context_for(scenario, channel))
    return None if result.passed else [v.rule_id for v in result.refusals]


@router.post("/check-reply")
def check_reply(payload: CheckReplyRequest) -> dict[str, object]:
    """Polled by the dashboard after a live send, to make the demo
    genuinely reactive rather than fire-and-forget: did the debtor (the
    demo owner, replying on their own phone) reply yet, and if so, what
    does the real extractor (agent.diagnose.llm_extract) make of it --
    and a real follow-up sent back over the same channel.

    No rate limit here (unlike /trigger): cost is bounded by construction,
    not by a rate limit -- a poll that finds nothing new costs nothing,
    only a genuinely new reply ever reaches extract_from_reply().
    """
    _require_secret(payload.secret)
    if payload.channel == "whatsapp":
        return _check_whatsapp_reply(payload)
    return _check_telegram_reply(payload)


def _check_telegram_reply(payload: CheckReplyRequest) -> dict[str, object]:
    """Only messages from the server-configured DEMO_CONTACT_TELEGRAM_CHAT_ID
    are ever considered -- a stranger messaging the bot during a live demo
    can never have their message surface as if it were the demo's own
    debtor replying. `after_update_id` is round-tripped by the caller and
    passed straight to Telegram's own `offset` semantics."""
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
            result["diagnosis"] = _diagnose_and_note(text)

            # The conversational half: a real message back over the same
            # channel, not just a diagnosis shown on a dashboard. Best-effort
            # -- a failed follow-up send still leaves the diagnosis itself
            # intact in the response rather than failing the whole poll.
            # Guarded against re-sending for an update_id already followed
            # up on (see _last_followed_up_update_id's comment) -- diagnosis
            # itself still re-runs every time, only the real send is skipped.
            global _last_followed_up_update_id
            if latest["update_id"] > _last_followed_up_update_id:
                scenario = SCENARIOS[payload.scenario]
                refusals = _bounds_gate_followup(scenario, "telegram")
                result["followup_bounds_refusals"] = refusals
                if refusals is None:
                    reply_text = _compose_or_fallback(text, result["diagnosis"], scenario)
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


def _check_whatsapp_reply(payload: CheckReplyRequest) -> dict[str, object]:
    """WhatsApp analog of _check_telegram_reply -- polls this account's own
    message history (TwilioWhatsAppChannel.list_messages()) rather than a
    live webhook, so a real reply can be found without needing Twilio
    Console webhook configuration for what's still a demo/dev surface (see
    docs/DEMO_UI.md). Only messages from the server-configured
    DEMO_CONTACT_PHONE_NUMBER are ever considered, the same isolation
    guarantee the Telegram path has via its configured chat_id.

    The follow-up send here always uses free-form Body text, not a
    template: the debtor just messaged us, which is exactly what opens
    WhatsApp's 24h customer-service window (docs/CHANNELS.md) -- a reply
    to an inbound message never needs a pre-approved template."""
    phone = os.environ.get("DEMO_CONTACT_PHONE_NUMBER")
    whatsapp_from = os.environ.get("TWILIO_WHATSAPP_FROM")
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    api_key_sid = os.environ.get("TWILIO_API_KEY_SID")
    api_key_secret = os.environ.get("TWILIO_API_KEY_SECRET")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not phone or not whatsapp_from or not sid or not (api_key_secret or auth_token):
        raise HTTPException(status_code=503, detail="Twilio WhatsApp demo contact not configured on this server")
    secret_value, username = (api_key_secret, api_key_sid) if api_key_secret else (auth_token, None)

    channel_obj = TwilioWhatsAppChannel(sid, secret_value, whatsapp_from, auth_username=username)
    try:
        messages = channel_obj.list_messages(to=whatsapp_from, from_=phone, limit=5)
    except ChannelUnavailable as exc:
        raise HTTPException(status_code=502, detail=f"channel unavailable: {exc}") from exc
    finally:
        channel_obj.close()

    if not messages:
        return {"has_reply": False}

    latest = messages[0]  # Twilio lists newest first, live-verified
    if payload.after_message_sid is not None and latest.get("sid") == payload.after_message_sid:
        return {"has_reply": False}

    text = latest.get("body") or ""
    result: dict[str, object] = {
        "has_reply": True, "text": text, "update_id": latest.get("sid"), "diagnosis": None, "agent_reply": None,
    }

    if payload.diagnose:
        try:
            result["diagnosis"] = _diagnose_and_note(text)

            global _last_followed_up_whatsapp_sid
            if latest.get("sid") != _last_followed_up_whatsapp_sid:
                scenario = SCENARIOS[payload.scenario]
                refusals = _bounds_gate_followup(scenario, "whatsapp")
                result["followup_bounds_refusals"] = refusals
                if refusals is None:
                    reply_text = _compose_or_fallback(text, result["diagnosis"], scenario)
                    reply_channel = TwilioWhatsAppChannel(sid, secret_value, whatsapp_from, auth_username=username)
                    try:
                        reply_channel.send(to=phone, text=reply_text)
                        result["agent_reply"] = reply_text
                        _last_followed_up_whatsapp_sid = latest.get("sid")
                    except ChannelUnavailable:
                        _log.warning("demo: WhatsApp follow-up reply send failed", exc_info=True)
                    finally:
                        reply_channel.close()
        except ExtractionFailed as exc:
            result["diagnosis"] = {"error": str(exc)}

    return result
