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
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agent.auditor.extraction_log import ExtractionLog
from agent.bounds.context import ActionCtx, BoundsContext, ConfigCtx, DebtorCtx, DecisionCtx, InvoiceCtx, MandateCtx
from agent.bounds.engine import check_bounds
from agent.diagnose.extract import Family
from agent.diagnose.llm_extract import ExtractionFailed, extract_from_reply
from agent.orchestrate import select_action_for_diagnosis
from agent.clock import business_now, business_today
from agent.debtor.invoices import (
    DISPUTED, OUTSTANDING, PAID, SCHEDULED, STATUS_LABEL, InvoiceStore,
)
from agent.debtor.registry import DebtorRegistry
from agent.debtor.seed import reset_invoices
from agent.debtor.score import BANDS, DebtorTerms, terms_for
from agent.mandate.emandate import create_plan_mandates, describe_mandate_links
from agent.mandate.health import check_mandate_health
from agent.mandate.portfolio import FAILURE_KINDS, MandatePortfolio, scan
from agent.mandate.payment_plan import PlanRejected, build_plan, describe_plan
from agent.money import to_rupees_display
from agent.notify.conversation import ConversationStore
from agent.notify.intents import detect_intent
from agent.notify.compose import ComposeFailed, compose_reply
from agent.notify.protocol import ChannelUnavailable
from agent.notify.telegram import TelegramChannel
from agent.notify.twilio_voice import TwilioVoiceChannel
from agent.notify.twilio_whatsapp import TwilioWhatsAppChannel
from agent.rails.razorpay_rail import RazorpayRail
from agent.rails.types import InvoiceSpec, LinkSpec, MandateSpec

_log = logging.getLogger("trucommit.demo")

router = APIRouter(prefix="/demo", tags=["demo"])

MIN_SECONDS_BETWEEN_TRIGGERS = 20.0
_last_triggered_at: dict[str, float] = {}

DEFAULT_WHATSAPP_CONTENT_SID = "HX7fab710a0f32ab9e8a1be21250bf98a3"
"""This project's own real WhatsApp Content Template
("truecommit_invoice_reminder_v2") -- a resource id, not a secret, so it
ships as the default rather than as one more env var to set on every
deployment. TWILIO_WHATSAPP_CONTENT_SID overrides it.

A cold WhatsApp send needs an approved template at all (free-form Body
only delivers inside the 24h customer-service window). Worth recording
why this is v2: v1 was rejected by Meta within seconds with "Variables
can't be at the start or end of the template" -- it ended with {{4}}, the
payment link. v2 puts real text after the link."""

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
    # Same "never contacted is None, not 0.0" reasoning as
    # _check_rate_limit above -- and with a 5-minute window this one was the
    # more damaging of the two.
    now = time.monotonic()
    last = _last_triggered_at_by_number.get(to)
    if last is not None and now - last < PER_NUMBER_COOLDOWN_SECONDS:
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
    # `None` for "never triggered", not 0.0. time.monotonic() counts from an
    # arbitrary origin -- on Linux, machine boot -- so a 0.0 default means
    # "triggered at boot", and on a freshly-started machine that reads as
    # *recent*. This rejected the first request after every restart for the
    # length of the window. Found by CI on Linux (a runner is seconds old);
    # invisible on a dev box that has been up for days. See
    # docs/WHAT_BROKE.md #11.
    now = time.monotonic()
    last = _last_triggered_at.get(channel)
    if last is not None and now - last < MIN_SECONDS_BETWEEN_TRIGGERS:
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
    creating a fresh object on every single click -- caught live: Razorpay's
    test-mode account has a hard 30-payment-link cap, and creating a new
    one per trigger burns through it in well under 30 clicks. One real
    payable URL per demo run is enough to prove the capability; recreating
    it repeatedly was never load-bearing for that.

    Falls back from a payment link to a real **invoice** when links are
    capped. Caught live: that 30-link cap counts lifetime creates, not live
    links -- cancelling old ones frees nothing, so on an exhausted account
    `create_payment_link` can never succeed again. Invoices have their own
    quota and are the better fit for this demo anyway: the scenario is an
    overdue *invoice*, so a real Razorpay-hosted invoice page is what a
    debtor would actually be sent. Both are real, payable, rail-created
    objects -- neither is a stand-in."""
    global _last_payment_link_url
    if _last_payment_link_url:
        return _last_payment_link_url
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        return None

    rail = RazorpayRail(key_id=key_id, key_secret=key_secret)
    description = f"TrueCommit demo -- {scenario['invoice_id']}"
    amount_paise = int(scenario["amount_paise"])

    try:
        link = rail.create_payment_link(LinkSpec(amount_paise=amount_paise, description=description))
        _last_payment_link_url = link.short_url
        return link.short_url
    except Exception:
        _log.warning("demo: payment link creation failed, trying a real invoice instead", exc_info=True)

    try:
        invoice = rail.create_invoice(InvoiceSpec(amount_paise=amount_paise, description=description))
        _last_payment_link_url = invoice.short_url
        return invoice.short_url
    except Exception:
        _log.warning("demo: real invoice creation failed too -- no payable URL available", exc_info=True)
        return None


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


def _bounds_context_for(
    scenario: dict[str, object], channel: str, *,
    uses_approved_template: bool = False, replying_to_inbound: bool = False,
) -> BoundsContext:
    """The two WhatsApp flags are not bookkeeping -- WHATSAPP_SESSION_WINDOW
    refuses a free-form send outside Meta's 24-hour window, and it can only
    tell the two apart if the caller says which it is doing.

    Adding the rule made every WhatsApp path in this module fail until each
    declared itself, which is the rule working: the code was sending
    templates and free-form replies through one undifferentiated context and
    the gate had no way to know.
    """
    return BoundsContext(
        debtor=DebtorCtx(id="demo_debtor", state="ENGAGED", touches_7d=0),
        mandate=MandateCtx(),
        action=ActionCtx(type="send_reminder", channel=channel, rail_tag="simulated",
                         uses_approved_template=uses_approved_template),
        decision=DecisionCtx(ev_paise=int(scenario["amount_paise"]) - 500),
        invoice=InvoiceCtx(id=str(scenario["invoice_id"]), recovery_attempts=1),
        config=ConfigCtx(),
        # A reply is inside the window by construction: it exists *because*
        # the debtor just messaged. Not an assumption -- the caller only
        # sets this on a path triggered by a real inbound message.
        last_inbound_at=datetime.now() if replying_to_inbound else None,
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

    # WhatsApp cold outreach goes out as an approved Content Template --
    # which is the only thing Meta permits outside the session window, and
    # what this path has always actually done (`send_template`, ContentSid).
    bounds_result = check_bounds(_bounds_context_for(
        scenario, payload.channel, uses_approved_template=payload.channel == "whatsapp"))
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
        content_sid = os.environ.get("TWILIO_WHATSAPP_CONTENT_SID", DEFAULT_WHATSAPP_CONTENT_SID)
        api_key_sid = os.environ.get("TWILIO_API_KEY_SID")
        api_key_secret = os.environ.get("TWILIO_API_KEY_SECRET")
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        if not sid or not whatsapp_from or not content_sid or not (api_key_secret or auth_token):
            raise HTTPException(status_code=503, detail="Twilio WhatsApp demo contact not configured on this server")
        phone = _resolve_recipient(payload.to, "DEMO_CONTACT_PHONE_NUMBER")
        secret_value, username = (api_key_secret, api_key_sid) if api_key_secret else (auth_token, None)
        channel_obj = TwilioWhatsAppChannel(sid, secret_value, whatsapp_from, auth_username=username)
        # No fake placeholder here. A WhatsApp template variable can't be
        # empty, and a made-up URL in a real message is worse than no
        # message at all -- so an unavailable payable URL fails the send
        # loudly instead. (Telegram degrades gracefully instead: it just
        # omits the "Pay now" line, since its body is free-form.)
        link_url = _create_real_payment_link(scenario)
        if not link_url:
            raise HTTPException(
                status_code=503,
                detail="no real payable URL could be created on the Razorpay account, and this template "
                       "requires one -- refusing to send a message with a placeholder link",
            )
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
        _record_event(
            _conversation_id_for(payload.channel, payload.to), kind="send_failed",
            channel=payload.channel, detail={"error": str(exc)},
        )
        raise HTTPException(status_code=502, detail=f"channel unavailable: {exc}") from exc
    finally:
        channel_obj.close()

    _record_event(
        _conversation_id_for(payload.channel, payload.to),
        kind="call_placed" if payload.channel == "ivr" else "message_sent",
        channel=payload.channel,
        detail={
            "status": result.status,
            "external_ref": result.external_ref,
            "invoice_id": str(scenario["invoice_id"]),
            "amount_paise": int(scenario["amount_paise"]),
            "bounds_passed": len([v for v in bounds_result.verdicts if v.verdict == "PASS"]),
            "text": text if payload.channel != "whatsapp" else str(scenario["text_message"]),
            "to": "the demo's own configured contact" if not payload.to else payload.to,
        },
    )

    return {
        "bounds_checks": [v.rule_id for v in bounds_result.verdicts if v.verdict == "PASS"],
        # Sent rather than assumed. The dashboard hardcoded "/19" and went
        # stale the moment a twentieth rule was added -- the same drift the
        # documented-test-count gate exists to stop elsewhere.
        "bounds_total": len(bounds_result.verdicts),
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


# How many times this conversation has already been chased. Real rules
# depend on it -- ATTEMPT_CEILING stops at six, CHANNEL_EXHAUSTION routes to
# a human -- so without it the demo could chase forever and never escalate,
# which is exactly the behaviour this project exists to argue against.
_conversation_touches: dict[str, int] = {}


def _diagnose_and_note(text: str, *, conversation_context: str | None = None):
    """The extraction half, shared by both channels: real extractor, real
    budget-tracked call. Returns the whole ExtractionResult rather than three
    flattened fields -- the promise's date and amount are on it, and DECIDE
    needs them (a stated promise is what PROMISE_COOLDOWN acts on)."""
    log_path = os.environ.get("TRUECOMMIT_EXTRACTION_LOG", "extraction_log.db")
    with ExtractionLog(log_path) as extraction_log:
        return extract_from_reply(text, purpose="demo_dashboard_live_reply", extraction_log=extraction_log,
                                  conversation_context=conversation_context)


def _diagnosis_dict(extraction) -> dict[str, object]:
    d: dict[str, object] = {
        "family": extraction.family.value,
        "class": extraction.class_.value,
        "confidence": extraction.confidence,
    }
    promise = getattr(extraction, "promise", None)
    if promise is not None and (promise.date or promise.amount_paise):
        d["promise"] = {"date": promise.date, "amount_paise": promise.amount_paise}
    return d


MIN_CONFIDENCE_FOR_PLAN = 0.65
"""Below this, a stated promise is acknowledged but not turned into a plan.

Calibrated against real extractions rather than picked round. Messages that
genuinely propose a schedule score high -- "21000 today and rest on 5th"
came back at 0.90, a three-leg split at 0.93. The ones that misled the
system scored low and honestly: "either 21000 today or the whole thing on
the 10th" at 0.55, "make it the 7th instead of the 5th" at 0.35-0.40.

The model was telling the truth about its own uncertainty and nothing was
listening. A plan built on a 0.4-confidence reading gets a real e-mandate
issued against it, which is a strange thing to do with a guess -- so the
threshold sits above the observed ambiguous band and below the observed
confident one, and a low-confidence promise gets an acknowledgement and a
question instead of an instrument.


"""


def _legs_from_schedule(promise, total: int) -> list[tuple[int, date]] | None:
    """The debtor's own multi-leg schedule, with "the rest" resolved.

    Returns None unless they genuinely named two or more dated payments, so
    every existing single-payment path is untouched.

    A leg with no amount is "the rest" -- "21,000 today and the balance on
    the 5th" is a real sentence, and the remainder is arithmetic this side
    owns (the model is told not to compute it, because inventing an amount
    the debtor did not say is exactly what `Promise` refuses elsewhere).

    Refuses rather than repairs when the numbers do not work: named amounts
    over the invoice total, more than one unnamed "rest", or a leg without a
    date. `build_plan` would reject the result anyway, and guessing at what
    they meant would put words in their mouth.
    """
    schedule = list(getattr(promise, "schedule", None) or [])
    if len(schedule) < 2:
        return None

    dated = [leg for leg in schedule if leg.date]
    if len(dated) != len(schedule):
        return None  # a leg with no date can't be scheduled against anything

    unnamed = [leg for leg in schedule if not leg.amount_paise]
    if len(unnamed) > 1:
        return None  # "some now and some later" is not a schedule

    named_total = sum(int(leg.amount_paise) for leg in schedule if leg.amount_paise)
    if named_total > total:
        return None  # they are describing a different debt than the invoice
    if not unnamed and named_total != total:
        return None  # fully-specified legs that don't sum are a real disagreement

    remainder = total - named_total
    if unnamed and remainder <= 0:
        return None  # nothing left for "the rest" to mean

    legs: list[tuple[int, date]] = []
    for leg in schedule:
        try:
            due = date.fromisoformat(str(leg.date))
        except ValueError:  # pragma: no cover -- PromiseLeg validates ISO8601 upstream
            return None
        legs.append((int(leg.amount_paise) if leg.amount_paise else remainder, due))
    return legs


def _plan_from_promise(extraction, scenario: dict[str, object],
                      terms: DebtorTerms | None = None,
                      outstanding_plan: bool = False) -> dict[str, object] | None:
    """A debtor who proposes a split gets a real, priced plan back.

    "I can do 21,000 on the 5th and the rest on the 20th" is the most
    common useful reply in collections and the one a dunning bot handles
    worst -- it either ignores the offer and repeats the full amount, or
    accepts it with no instrument behind it. Everything needed to answer
    properly already existed (`select_instrument`, `compute_early_payment_offer`,
    `Promise.installments`, which the extractor has always populated); this
    reads what they proposed and asks those.

    A date is what makes it a plan. Two shapes come out of that:

      - **split** -- they named a part-payment ("21,000 on the 5th"), so
        leg 1 is theirs and the balance is proposed a fortnight on, marked
        `proposed_by: system` so the reply can put it as a proposal rather
        than imply they agreed to it.
      - **full** -- they named a date and either the whole balance or no
        amount at all ("I'll pay on the 5th"). That is a one-instalment
        plan on *their own* date, and it earns a real e-mandate link the
        same way a split does. Nothing is invented here: assuming the full
        balance when they didn't name one is the conservative reading, and
        the date is quoted straight back from what they said.

      - **stated** -- they named the whole schedule themselves ("21,000
        today and the rest on the 5th"). Every leg is theirs; nothing is
        proposed. This is the shape that should happen most often and was
        impossible before `promise.schedule` existed.

    A promise with no date at all is not a plan -- there is nothing to
    schedule a debit against -- and gets the ordinary path.
    """
    promise = getattr(extraction, "promise", None)
    if promise is None or not promise.date:
        return None

    if getattr(extraction, "confidence", 1.0) < MIN_CONFIDENCE_FOR_PLAN:
        # The extractor is uncertain what was proposed, and says so. Issuing
        # a real mandate against a guess is worse than asking.
        _log.info("demo: confidence %.2f is below %.2f -- acknowledging without building a plan",
                  extraction.confidence, MIN_CONFIDENCE_FOR_PLAN)
        return None

    total = int(scenario["amount_paise"])
    try:
        first_date = date.fromisoformat(promise.date)
    except ValueError:  # pragma: no cover -- ExtractionResult validates ISO8601 upstream
        return None

    terms = terms or terms_for([])

    # What the debtor actually said, if they said more than one thing.
    proposed_legs = len(getattr(promise, "schedule", None) or [])
    stated_legs = _legs_from_schedule(promise, total)

    if stated_legs is None and proposed_legs >= 2:
        # They proposed several payments and the schedule could not be made
        # to add up -- "half now and half at month end" names no amounts,
        # "50,000 on the 5th and 20,000 later" exceeds the invoice.
        #
        # Falling through from here is what turned a careful refusal into a
        # confident misreading: `stated = amount or total` reads a missing
        # amount as the full balance, so the system answered "half now and
        # half at month end" by offering the entire Rs 42,500 today. Nobody
        # said that. Building no plan leaves the composer to acknowledge
        # what they proposed and ask for the missing numbers, which is the
        # honest reply to an offer that doesn't yet add up.
        _log.info("demo: %d proposed legs could not be reconciled -- not building a plan", proposed_legs)
        return None

    if (stated_legs is None and not promise.amount_paise
            and outstanding_plan and proposed_legs == 0):
        # A bare date with a plan already on the table is a *change* to that
        # plan -- "make it the 7th instead of the 5th" -- not a new promise
        # to pay everything on the 7th. Reading it as the latter offered the
        # full balance to a debtor who was moving one instalment by two days.
        #
        # Deliberately no plan rather than a guessed re-date: which leg they
        # meant is genuinely ambiguous, and the composer already receives the
        # outstanding proposal and can put the question back to them.
        _log.info("demo: a bare date against an outstanding plan is a change to it, not a new promise")
        return None
    if stated_legs is not None and len(stated_legs) > terms.max_instalments:
        # Their record does not stretch to that many instalments. Falling
        # through rather than silently truncating their schedule: dropping a
        # leg they named would misrepresent the offer back to them.
        _log.info("demo: %d stated legs exceeds the %s band's %d -- not offering their schedule",
                  len(stated_legs), terms.band, terms.max_instalments)
        stated_legs = None
    if stated_legs is not None and terms.offers_instalment_plan:
        shape, legs = "stated", stated_legs
    else:
        stated = int(promise.amount_paise) if promise.amount_paise else total
        if stated >= total:
            shape, legs = "full", [(total, first_date)]
        elif not terms.offers_instalment_plan:
            # A debtor in the strict band is not offered a split. Their own
            # record is the reason, and the reply says so rather than going
            # quiet: the full amount on their date is still a plan, and still
            # earns a real mandate.
            shape, legs = "full", [(total, first_date)]
        else:
            # The debtor named one leg and a date. The remainder is theirs to
            # place; absent a second stated date, this proposes the balance a
            # fortnight on.
            shape, legs = "split", [(stated, first_date), (total - stated, first_date + timedelta(days=14))]

    try:
        plan = build_plan(invoice_id=str(scenario["invoice_id"]), total_amount_paise=total,
                          legs=legs, discount_rate=terms.early_discount_rate)
    except PlanRejected as exc:
        _log.warning("demo: could not build a plan from the stated promise: %s", exc)
        return None

    result: dict[str, object] = {
        "shape": shape,
        "debtor_band": terms.band,
        "debtor_credibility": terms.credibility,
        "instalment_plan_offered": terms.offers_instalment_plan,
        "legs": [
            {
                "sequence": leg.sequence,
                "amount_paise": leg.amount_paise,
                "due_date": leg.due_date.isoformat(),
                "payable_paise": leg.payable_paise,
                "savings_paise": leg.savings_paise,
                "proposed_by": "debtor" if shape in ("full", "stated") or leg.sequence == 1 else "system",
            }
            for leg in plan.legs
        ],
        # Both, deliberately: what §12.2 recommends and what this account
        # can actually issue. Reporting only the recommendation is what let
        # the demo claim UPI block-and-reserve while creating an e-mandate.
        "instrument": plan.deployment.deployable.value,
        "recommended_instrument": plan.deployment.recommended.value,
        "instrument_substituted": plan.deployment.substituted,
        "instrument_note": plan.deployment.reason,
        "requires_afa_per_debit": plan.requires_afa_per_debit,
        "total_payable_paise": plan.total_payable_paise,
        "total_savings_paise": plan.total_savings_paise,
        "summary": describe_plan(plan),
    }

    links = _mandate_links_for(plan)
    if links:
        result["mandate_links"] = [
            {
                "mandate_id": link.mandate_id,
                "short_url": link.short_url,
                "amount_paise": link.amount_paise,
                "sequences": list(link.sequences),
                "first_debit_on": link.first_debit_on.isoformat(),
                "afa_required": link.afa_required,
            }
            for link in links
        ]
        result["mandate_summary"] = describe_mandate_links(links)
    return result


# Real mandates already created this run, keyed by the plan's shape. Same
# reasoning as `_last_payment_link_url`: these are real rail objects, and
# minting a fresh Plan + Subscription pair on every message a debtor sends
# would litter the account with dozens of unauthorized mandates for one
# negotiation. An identical plan re-derived from a repeated message reuses
# the links the debtor was already sent -- which is also what they expect,
# since a second different link for the same instalment is confusing.
_mandate_link_cache: dict[str, list] = {}


def _plan_signature(plan) -> str:
    return "|".join(f"{leg.sequence}:{leg.payable_paise}:{leg.due_date.isoformat()}" for leg in plan.legs)


def _mandate_links_for(plan) -> list:
    """Real, authorizable e-mandate links for a plan -- or [] if the rail
    can't produce them.

    Best-effort by design: a missing mandate link degrades the reply (the
    composer is simply told there isn't one) rather than failing the whole
    exchange and leaving the debtor with silence. What it must never do is
    substitute a plausible-looking URL -- `MandateCreationFailed` is caught
    and logged, never papered over.
    """
    signature = _plan_signature(plan)
    cached = _mandate_link_cache.get(signature)
    if cached is not None:
        return cached

    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        return []

    try:
        links = create_plan_mandates(plan, rail=RazorpayRail(key_id=key_id, key_secret=key_secret))
    except Exception:
        _log.warning("demo: could not create e-mandate links for this plan", exc_info=True)
        return []

    _mandate_link_cache[signature] = links
    return links


REFUSALS_THAT_MEAN_WAIT = frozenset({"PROMISE_COOLDOWN", "RBI_FPC_HOURS", "EV_FLOOR"})
"""Refusals whose answer is "not now", not "hand this to a person".

A promise buys quiet time, contact hours reopen in the morning, and a
case under the EV floor is one the system has decided not to chase --
`EV_FLOOR` exists precisely so that decision is logged rather than
silent (README's third claim). Escalating any of these to a human would
be treating a healthy case as a problem, and would bury the queue a real
escalation needs to stay useful.

Every other refusal does mean a person should look: a dispute, an
exhausted channel, a statutory gate, a ceiling reached.
"""


def _decide_next_step(extraction, scenario: dict[str, object], *, channel: str, debtor_key: str,
                      terms: DebtorTerms | None = None) -> dict[str, object]:
    """DECIDE -> BOUNDS on a diagnosed reply, through the real machinery.

    This is the half the conversational demo was missing. It used to
    diagnose a reply, compose an answer, and stop -- so "I'll pay on the
    14th" got a polite sentence and nothing else: no promise recorded, no
    cooldown, no escalation, and a payment link offered to someone who had
    just asked for time. The rules that make that right already existed and
    simply weren't being consulted.

    What actually runs here:
      - `select_action_for_diagnosis()` -- the same mapping the webhook
        orchestrator uses. Family C asks for a reminder, D routes to a
        human, B reissues the artifact, A offers a fresh instrument.
      - `check_bounds()` on that action, with a context that reflects the
        conversation: PROMISED with the debtor's own stated date (so
        PROMISE_COOLDOWN can buy them quiet time), DISPUTED_FROZEN on a
        dispute, and the running touch count (so ATTEMPT_CEILING and
        CHANNEL_EXHAUSTION eventually bite).
      - If the gate refuses, the fallback is `escalate_human` -- not
        silence, and not the refused action anyway.
    """
    action_type = select_action_for_diagnosis(extraction)
    terms = terms or terms_for([])
    touches = _conversation_touches.get(debtor_key, 0)

    promise = getattr(extraction, "promise", None)
    promise_date = None
    debtor_state = "ENGAGED"
    if extraction.family == Family.D:
        debtor_state = "DISPUTED_FROZEN"
    elif promise is not None and promise.date:
        debtor_state = "PROMISED"
        try:
            promise_date = datetime.fromisoformat(promise.date)
        except ValueError:  # pragma: no cover -- ExtractionResult validates ISO8601 upstream
            promise_date = None

    def _gate(candidate: str):
        ctx = BoundsContext(
            debtor=DebtorCtx(id=debtor_key, state=debtor_state, touches_7d=touches,
                             promise_credibility=terms.credibility),
            mandate=MandateCtx(),
            action=ActionCtx(type=candidate, channel=None if candidate == "escalate_human" else channel,
                             rail_tag="simulated"),
            decision=DecisionCtx(ev_paise=int(scenario["amount_paise"]) - 500),
            invoice=InvoiceCtx(id=str(scenario["invoice_id"]), recovery_attempts=touches),
            config=ConfigCtx(grace_days=terms.grace_days),
            promise_date=promise_date,
        )
        return check_bounds(ctx)

    result = _gate(action_type.value)
    chosen, refusals, escalated = action_type.value, [v.rule_id for v in result.refusals], False

    # The rule's own words, not just its id. `BoundsVerdict.reason` is
    # already `rule.human` on a refusal, so the plain-language explanation
    # existed all along and was being thrown away at this boundary -- which
    # is why a refusal rendered in the dashboard as a bare identifier a
    # viewer had to already know the meaning of.
    refusal_detail = [{"rule_id": v.rule_id, "reason": v.reason} for v in result.refusals]
    rules_passed = sum(1 for v in result.verdicts if v.verdict == "PASS")
    rules_total = len(result.verdicts)

    if not result.passed:
        if refusals and set(refusals) <= REFUSALS_THAT_MEAN_WAIT:
            # Not every refusal means "this needs a person". A debtor who
            # just named a date is a *good* outcome, and escalating them to
            # a human over-reacts to what the cooldown was asking for --
            # which is simply quiet time. Waiting is the answer, and
            # `no_action` is how this system says that out loud.
            chosen = "no_action"
        else:
            # The gate refused for a reason that does need a person.
            # Escalating is the designed answer -- a refusal is a routing
            # decision, not a dead end.
            escalated_result = _gate("escalate_human")
            if escalated_result.passed:
                chosen, escalated = "escalate_human", True
            else:
            # Both refused. The one thing this must not do is fall back to
            # the action the gate just refused -- which is what it used to
            # do, while reporting it as `allowed`. Observed live: a debtor
            # stating a promise put them in PROMISED, PROMISE_COOLDOWN
            # refused every action, and `send_reminder` was reported as the
            # allowed next step anyway.
            #
                # `no_action` is the honest answer, and it is a
                # first-class logged decision rather than silence
                # (README's third claim).
                chosen = "no_action"

    _conversation_touches[debtor_key] = touches + 1
    return {
        "action": chosen,
        "proposed_action": action_type.value,
        # Whether the *gate* allowed the proposed action -- not whether the
        # chosen action happens to equal it. Those differ exactly when
        # everything was refused, which is the case worth reporting
        # accurately.
        "allowed": result.passed,
        "escalated_to_human": escalated,
        "refusals": refusals,
        "refusal_detail": refusal_detail,
        # The tally the dashboard needs to say "18/19 passed, 1 refused"
        # rather than showing a refusal with no sense of scale.
        "rules_passed": rules_passed,
        "rules_total": rules_total,
        "touches_before": touches,
        "debtor_state": debtor_state,
        "promise_date": promise.date if promise is not None else None,
        # What the debtor's own record bought them here -- the grace period
        # PROMISE_COOLDOWN just applied is `grace_days * credibility`.
        "debtor_band": terms.band,
        "debtor_credibility": terms.credibility,
        "grace_days": terms.grace_days,
    }


# Why the most recent composition fell back, if it did. Read once by
# `handle_inbound_message` immediately after the call and then cleared, so
# a stale reason can never be attributed to a later, successful reply.
_last_compose_failure: dict[str, str] = {}


def _take_compose_failure() -> str | None:
    return _last_compose_failure.pop("reason", None)


def _compose_or_fallback(
    reply_text: str, diagnosis: dict[str, object], scenario: dict[str, object],
    decision: dict[str, object] | None = None, plan: dict[str, object] | None = None,
    conversation_context: str | None = None, outstanding_proposal: str | None = None,
) -> str:
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
            # The link is only offered when the gate actually allowed an
            # action that involves one. Someone who just asked for time gets
            # acknowledged, not handed a payment link -- which is the whole
            # point of consulting DECIDE before writing the reply.
            payment_link=(
                _last_payment_link_url
                if decision is None or decision.get("action") in ("create_payment_link", "send_reminder")
                else None
            ),
            next_step=None if decision is None else str(decision.get("action")),
            payment_plan=None if plan is None else str(plan.get("summary")),
            mandate_links=None if plan is None else (plan.get("mandate_summary") or None),
            conversation_context=conversation_context,
            outstanding_proposal=outstanding_proposal,
            purpose="demo_dashboard_conversational_reply",
        )
    except Exception as exc:
        # Broad on purpose: ComposeFailed (API/empty output) and
        # BudgetExceeded (agent.spend's ceiling) both mean "no vetted reply
        # available right now", and neither should break a read-only poll.
        #
        # The reason is stashed rather than only logged. A live run fell back
        # to the fixed line and the only record was a Render log line nobody
        # reads -- so a debtor got a generic "here's the link again" instead
        # of the plan and mandate links just built for them, and diagnosing
        # it afterwards was guesswork. A degradation that leaves no trace is
        # the same invisible-absence problem a refused action has.
        _log.warning("demo: contextual reply composition failed, falling back to the fixed line: %s", exc)
        _last_compose_failure["reason"] = f"{type(exc).__name__}: {exc}"[:500]
        return _agent_reply_for(family)


def _bounds_gate_followup(scenario: dict[str, object], channel: str) -> list[str] | None:
    """The same check_bounds() gate /trigger runs, applied to the
    conversational follow-up too -- a reply is an outbound contact like any
    other, and exempting it because it happens to be a response would be
    exactly the kind of quiet carve-out this project exists to not have.
    Returns the refusing rule ids, or None when the send is allowed."""
    result = check_bounds(_bounds_context_for(scenario, channel, replying_to_inbound=True))
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
            extraction = _diagnose_and_note(text)
            result["diagnosis"] = _diagnosis_dict(extraction)

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
                    decision = _decide_next_step(
                        extraction, scenario, channel="telegram", debtor_key=f"demo_telegram_{payload.scenario}",
                    )
                    result["decision"] = decision
                    plan = _plan_from_promise(extraction, scenario)
                    if plan is not None:
                        result["payment_plan"] = plan
                    reply_text = _compose_or_fallback(text, result["diagnosis"], scenario, decision, plan)
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
            extraction = _diagnose_and_note(text)
            result["diagnosis"] = _diagnosis_dict(extraction)

            global _last_followed_up_whatsapp_sid
            if latest.get("sid") != _last_followed_up_whatsapp_sid:
                scenario = SCENARIOS[payload.scenario]
                refusals = _bounds_gate_followup(scenario, "whatsapp")
                result["followup_bounds_refusals"] = refusals
                if refusals is None:
                    decision = _decide_next_step(
                        extraction, scenario, channel="whatsapp", debtor_key=f"demo_whatsapp_{payload.scenario}",
                    )
                    result["decision"] = decision
                    plan = _plan_from_promise(extraction, scenario)
                    if plan is not None:
                        result["payment_plan"] = plan
                    reply_text = _compose_or_fallback(text, result["diagnosis"], scenario, decision, plan)
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


# --------------------------------------------------------------------------
# The one path an inbound debtor message takes, whatever delivered it.
# --------------------------------------------------------------------------

def _conversation_store() -> ConversationStore:
    return ConversationStore(os.environ.get("TRUECOMMIT_CONVERSATION_DB", "conversation.db"))


def _channel_ref_of(conversation_id: str) -> str:
    """The channel address behind a possibly-namespaced conversation id.

    `sub:8327566456` and `8327566456` are two threads with one person
    behind them: Telegram's private-chat id is the *user's* id, so both
    bots report the same number for the same human.

    The distinction this draws is the important one. Conversation state --
    transcript, outstanding proposal, handled-message claims -- is
    per-thread, because a plan offered on one bot must not be acceptable on
    the other. Identity and record are per-*person*: someone who breaks a
    promise about their subscription has broken a promise, and their
    credibility should not reset because a different bot carried it.
    """
    if conversation_id.startswith(SUBSCRIPTION_THREAD_PREFIX):
        return conversation_id[len(SUBSCRIPTION_THREAD_PREFIX):]
    return conversation_id


def _registry() -> DebtorRegistry:
    return DebtorRegistry(os.environ.get("TRUECOMMIT_DEBTORS_DB", "debtors.db"))


def _terms_for_conversation(conversation_id: str) -> tuple[str | None, DebtorTerms]:
    """This debtor's id and the terms their own record earns.

    Falls back to a no-history score for an unknown channel rather than
    refusing to act: someone messaging the bot who isn't in the register is
    a new debtor, and a new debtor gets the benefit of the doubt (see
    `agent.debtor.score.NO_HISTORY_CREDIBILITY`).
    """
    try:
        registry = _registry()
        try:
            # The person, not the thread -- see `_channel_ref_of`. Looking
            # up the namespaced id found no debtor at all, so the
            # subscription conversation scored on no-history defaults and
            # recorded no promises against the person who made them.
            debtor = registry.by_channel_ref(_channel_ref_of(conversation_id))
            if debtor is None:
                return None, terms_for([])
            # Time passing is what breaks a promise, so resolve overdue ones
            # before reading the score rather than letting a stale 'pending'
            # flatter the debtor indefinitely.
            registry.expire_overdue_promises(debtor.id)
            return debtor.id, registry.terms(debtor.id)
        finally:
            registry.close()
    except Exception:
        _log.warning("demo: could not read debtor terms -- using no-history defaults", exc_info=True)
        return None, terms_for([])


def _conversation_id_for(channel: str, to: str | None = None) -> str:
    """The key a channel's exchanges are filed under.

    Telegram's is the chat id, because that is what its webhook reports and
    the two must agree or an inbound reply lands on a different timeline
    from the message it answers. The phone channels key on the number
    actually dialled."""
    if channel == "telegram":
        return os.environ.get("DEMO_CONTACT_TELEGRAM_CHAT_ID", "telegram")
    return to or os.environ.get("DEMO_CONTACT_PHONE_NUMBER", channel)


def _record_event(conversation_id: str, *, kind: str, channel: str | None = None,
                  detail: dict | None = None) -> None:
    """Append to the timeline, swallowing storage failures.

    Deliberately best-effort: this is the observability record, and losing
    a row from it is a bad outcome, but taking down a real send or a real
    reply because the timeline couldn't be written is a far worse one."""
    try:
        store = _conversation_store()
        try:
            store.record_event(conversation_id, kind=kind, channel=channel, detail=detail)
        finally:
            store.close()
    except Exception:
        _log.warning("demo: could not record a %r timeline event", kind, exc_info=True)


def _describe_proposal(proposal) -> str:
    """One line naming what is currently on the table, for the composer."""
    if proposal.kind == "payment_plan":
        legs = proposal.detail.get("legs", [])
        parts = [f"Rs {leg['amount_paise'] / 100:,.0f} on {leg['due_date']}" for leg in legs]
        return "an instalment plan of " + " then ".join(parts)
    return proposal.kind


def _record_stated_promise(debtor_id: str | None, plan: dict, scenario: dict[str, object]) -> None:
    """A stated instalment becomes a pending promise on the debtor's record.

    This is what makes the score move. Pending, not kept -- only a
    rail-confirmed capture keeps a promise (Law 7's standard, applied by
    `settle_promise`), and only the date passing without one breaks it.
    Saying it convincingly does neither.
    """
    if debtor_id is None:
        return
    try:
        registry = _registry()
        try:
            for leg in plan.get("legs", []):
                if leg.get("proposed_by") != "debtor":
                    continue  # a date we proposed is not a promise they made
                registry.record_promise(
                    debtor_id, invoice_id=str(scenario["invoice_id"]),
                    amount_paise=int(leg["amount_paise"]), promised_date=str(leg["due_date"]),
                )
        finally:
            registry.close()
    except Exception:
        _log.warning("demo: could not record the stated promise", exc_info=True)



# --------------------------------------------------------------------------
# Self-service: a debtor asking about their own invoices.
# --------------------------------------------------------------------------

def _invoice_store() -> InvoiceStore:
    return InvoiceStore(os.environ.get("TRUECOMMIT_DEBTORS_DB", "debtors.db"))


def _resolve_invoice(invoices: list, ref: str | None, focus: str | None):
    """Which invoice the debtor means.

    A bare number is a position in the list they were just shown, so it is
    resolved against that same ordering rather than a database id -- "2"
    must mean the line printed as 2, not whatever row happens to be second
    in storage.
    """
    if ref:
        for inv in invoices:
            if inv.invoice_id.upper() == ref.upper():
                return inv
        if ref.isdigit():
            index = int(ref) - 1
            if 0 <= index < len(invoices):
                return invoices[index]
        return None
    if focus:
        return next((i for i in invoices if i.invoice_id == focus), None)
    # Exactly one thing is open, so there is nothing to disambiguate.
    open_invoices = [i for i in invoices if i.is_open]
    return open_invoices[0] if len(open_invoices) == 1 else None


def _render_invoice_list(invoices: list, store: InvoiceStore, debtor_id: str) -> str:
    if not invoices:
        return ("I cannot see any invoices against this account. If you are expecting one, "
                "reply and a person will check.")
    lines = ["Here is everything on your account:", ""]
    for position, inv in enumerate(invoices, start=1):
        overdue = inv.days_overdue()
        status = STATUS_LABEL.get(inv.status, inv.status)
        if inv.status == OUTSTANDING and overdue:
            detail = f"{status}, {overdue} days overdue"
        elif inv.status == OUTSTANDING:
            detail = f"{status} {inv.due_date}"
        else:
            detail = status
        lines.append(f"{position}. {inv.invoice_id} -- {to_rupees_display(inv.amount_paise)} ({detail})")

    outstanding = store.total_outstanding_paise(debtor_id)
    lines.append("")
    if outstanding:
        lines.append(f"Outstanding: {to_rupees_display(outstanding)}.")
        lines.append("Reply with a number to pick one, then: schedule, dispute, or problem "
                     "to reach a person.")
    else:
        lines.append("Nothing outstanding -- all clear.")
    return "\n".join(lines)


def _describe_invoice(invoice) -> str:
    overdue = invoice.days_overdue()
    if invoice.status == PAID:
        return (f"{invoice.invoice_id} -- {to_rupees_display(invoice.amount_paise)} is paid and "
                "settled. Nothing outstanding on it.")
    if invoice.status == DISPUTED:
        return (f"{invoice.invoice_id} -- {to_rupees_display(invoice.amount_paise)} is marked "
                "disputed and a person has it. Automated chasing on it is paused.")
    when = f", {overdue} days overdue" if overdue else f", due {invoice.due_date}"
    return (f"{invoice.invoice_id} -- {to_rupees_display(invoice.amount_paise)}{when}.\n\n"
            "Reply: schedule to set up payment, dispute if something is wrong with it, "
            "or problem to reach a person.")


def _offer_schedule(invoice, conversation_id: str, channel: str, store, invoice_store) -> dict:
    """A real mandate for the whole invoice on its own due date.

    Deliberately the simplest possible plan. They asked to schedule; they
    did not propose terms, and inventing a split here would be putting a
    schedule in their mouth. The negotiation path already handles the case
    where they name one.
    """
    if invoice.status == PAID:
        return {"reply": f"{invoice.invoice_id} is already paid -- nothing to schedule.",
                "intent": "schedule_noop"}
    if invoice.status == DISPUTED:
        return {"reply": f"{invoice.invoice_id} is disputed and with a person, so I am not "
                         "setting up a debit on it. They will come back to you.",
                "intent": "schedule_blocked"}

    due = max(date.fromisoformat(invoice.due_date), business_today() + timedelta(days=1))
    try:
        plan = build_plan(invoice_id=invoice.invoice_id,
                          total_amount_paise=invoice.amount_paise,
                          legs=[(invoice.amount_paise, due)])
    except PlanRejected as exc:
        _log.warning("demo: could not build a schedule for %s: %s", invoice.invoice_id, exc)
        return {"reply": "I could not set that up automatically -- passing it to a person.",
                "intent": "schedule_failed"}

    links = _mandate_links_for(plan)
    if not links:
        # No plausible-looking URL, ever. An unavailable link means saying so.
        return {"reply": f"I could not create the mandate link just now. A person will follow "
                         f"up on {invoice.invoice_id} -- nothing has been charged.",
                "intent": "schedule_no_link"}

    invoice_store.set_status(invoice.debtor_id, invoice.invoice_id, SCHEDULED,
                             note=f"mandate {links[0].mandate_id}")
    store.record_event(conversation_id, kind="mandate_issued", channel=channel, detail={
        "links": [{"mandate_id": link.mandate_id, "short_url": link.short_url,
                   "amount_paise": link.amount_paise, "sequences": list(link.sequences),
                   "first_debit_on": link.first_debit_on.isoformat(),
                   "afa_required": link.afa_required} for link in links],
        "invoice_id": invoice.invoice_id, "via": "self_service",
    })
    leg = plan.legs[0]
    return {
        "reply": (f"Set up for {invoice.invoice_id}: {to_rupees_display(leg.payable_paise)} on "
                  f"{leg.due_date.isoformat()}.\n\nAuthorize it here: {links[0].short_url}\n\n"
                  "That schedules the debit for that date. It does not take anything now."),
        "intent": "schedule", "invoice_id": invoice.invoice_id,
    }


def _raise_dispute(invoice, conversation_id: str, channel: str, store, invoice_store) -> dict:
    """Freeze the line and route it to a person.

    No attempt to judge whether the dispute is valid. That is the whole
    point of DISPUTE_FREEZE: a contested amount stops being chased while a
    human looks, and a system deciding for itself which disputes counted
    would be doing the exact thing the rule exists to prevent.
    """
    if invoice.status == PAID:
        return {"reply": f"{invoice.invoice_id} is already settled. Tell me what is wrong with "
                         "it and I will pass it to a person.", "intent": "dispute_on_paid"}

    invoice_store.set_status(invoice.debtor_id, invoice.invoice_id, DISPUTED,
                             note="raised by the debtor in conversation")
    store.record_event(conversation_id, kind="dispute_raised", channel=channel,
                       detail={"invoice_id": invoice.invoice_id,
                               "amount_paise": invoice.amount_paise})
    return {
        "reply": (f"Noted -- {invoice.invoice_id} is marked disputed and automated chasing on "
                  "it has stopped. A person will review it.\n\nTell me what is wrong with it "
                  "and I will pass that on with it."),
        "intent": "dispute", "invoice_id": invoice.invoice_id,
    }


def _handle_intent(intent, *, conversation_id: str, channel: str, store) -> dict | None:
    """Answer a menu command without a model call.

    Returns None when the command cannot be answered here -- no debtor on
    record, no invoices -- so the message falls through to the ordinary
    diagnosed path rather than dead-ending. A debtor who types something
    slightly off should still be read, not rejected.
    """
    debtor_id, _terms = _terms_for_conversation(conversation_id)
    if debtor_id is None:
        return None

    invoice_store = _invoice_store()
    try:
        invoices = invoice_store.for_debtor(debtor_id)
        if not invoices:
            return None

        if intent.kind in ("list", "help"):
            store.record_event(conversation_id, kind="invoices_listed", channel=channel,
                               detail={"count": len(invoices)})
            return {"reply": _render_invoice_list(invoices, invoice_store, debtor_id),
                    "intent": intent.kind}

        invoice = _resolve_invoice(invoices, intent.invoice_ref, store.focus(conversation_id))
        if invoice is None:
            if intent.kind == "select":
                return {"reply": "I could not match that to one of your invoices. Reply "
                                 "'invoices' to see the list again.", "intent": "select_miss"}
            return {"reply": "Which invoice is that about? Reply 'invoices' for the list, "
                             "then the number.", "intent": f"{intent.kind}_needs_invoice"}

        store.set_focus(conversation_id, invoice.invoice_id)

        if intent.kind == "select":
            return {"reply": _describe_invoice(invoice), "intent": "select",
                    "invoice_id": invoice.invoice_id}
        if intent.kind == "schedule":
            return _offer_schedule(invoice, conversation_id, channel, store, invoice_store)
        if intent.kind == "dispute":
            return _raise_dispute(invoice, conversation_id, channel, store, invoice_store)
        if intent.kind == "problem":
            # No diagnosis attempted. They asked for a person, and guessing
            # at the problem first answers a question they did not ask.
            store.record_event(conversation_id, kind="escalated_by_request", channel=channel,
                               detail={"invoice_id": invoice.invoice_id})
            return {"reply": f"Understood -- I have flagged {invoice.invoice_id} for a person "
                             "to pick up, and paused automated messages on it. Anything you "
                             "add here goes to them with it.",
                    "intent": "problem", "invoice_id": invoice.invoice_id}
    finally:
        invoice_store.close()
    return None

def handle_inbound_message(
    *,
    conversation_id: str,
    external_id: str,
    text: str,
    channel: str,
    scenario_key: str = "b2b",
    send,
) -> dict[str, object]:
    """Diagnose, decide, plan, compose, reply -- with memory.

    Both the Telegram webhook and the dashboard's polling endpoint call
    this, so a message is handled identically however it arrived. `send` is
    injected rather than chosen here: the caller already holds an open,
    authenticated channel, and this function has no business picking one.

    The message is claimed before anything else happens. A webhook
    redelivery, a poller racing the webhook, or a restart mid-handle would
    otherwise answer the same message twice -- and unlike diagnosis, a reply
    is not free to repeat. The UNIQUE constraint decides that, not a prior
    read.
    """
    scenario = SCENARIOS[scenario_key]
    store = _conversation_store()
    try:
        if not store.claim_message(conversation_id, external_id):
            return {"handled": False, "reason": "already_handled", "external_id": external_id}

        # Read the history *before* recording this turn, so the transcript
        # is what came before rather than including the message itself.
        proposal = store.outstanding_proposal(conversation_id)
        transcript = store.transcript(conversation_id)
        store.record_turn(conversation_id, direction="inbound", text=text)
        store.record_event(conversation_id, kind="reply_received", channel=channel, detail={"text": text})

        result: dict[str, object] = {"handled": True, "text": text, "external_id": external_id}

        # A menu command is answered here, before any model call. "2" or
        # "dispute" needs no language understanding, and routing it through
        # an extractor would cost four seconds and a chance of being read as
        # something the debtor did not ask for. Anything that is not
        # unambiguously a command returns None and takes the ordinary path.
        intent = detect_intent(text)
        if intent is not None:
            answered = _handle_intent(intent, conversation_id=conversation_id,
                                      channel=channel, store=store)
            if answered is not None:
                reply_text = str(answered["reply"])
                try:
                    send(reply_text)
                except ChannelUnavailable:
                    _log.warning("inbound: reply send failed on %s", channel, exc_info=True)
                    store.record_event(conversation_id, kind="reply_send_failed", channel=channel)
                    return result
                store.record_turn(conversation_id, direction="outbound", text=reply_text)
                store.record_event(conversation_id, kind="agent_replied", channel=channel,
                                   detail={"text": reply_text, "intent": answered.get("intent")})
                result.update({"agent_reply": reply_text, "self_service": answered.get("intent")})
                return result

        try:
            extraction = _diagnose_and_note(text, conversation_context=transcript or None)
        except ExtractionFailed as exc:
            result["diagnosis"] = {"error": str(exc)}
            store.record_event(conversation_id, kind="extraction_failed", channel=channel,
                               detail={"error": str(exc)})
            return result

        result["diagnosis"] = _diagnosis_dict(extraction)
        store.record_event(conversation_id, kind="diagnosed", channel=channel, detail=result["diagnosis"])

        # What this debtor's own track record earns them -- read once and
        # passed down, so the cooldown, the plan's discount and the number
        # of instalments offered all reflect the same score.
        debtor_id, terms = _terms_for_conversation(conversation_id)
        result["debtor"] = {
            "id": debtor_id, "band": terms.band, "credibility": terms.credibility,
            "grace_days": terms.grace_days, "max_instalments": terms.max_instalments,
            "rationale": terms.rationale,
        }

        refusals = _bounds_gate_followup(scenario, channel)
        result["followup_bounds_refusals"] = refusals
        if refusals is not None:
            store.record_event(conversation_id, kind="bounds_refused", channel=channel,
                               detail={"refusals": refusals})
            return result

        decision = _decide_next_step(
            extraction, scenario, channel=channel, debtor_key=f"{channel}_{conversation_id}",
            terms=terms,
        )
        result["decision"] = decision
        store.record_event(conversation_id, kind="decided", channel=channel, detail=decision)

        plan = _plan_from_promise(
            extraction, scenario, terms,
            outstanding_plan=proposal is not None and proposal.kind == "payment_plan",
        )
        if plan is not None:
            _record_stated_promise(debtor_id, plan, scenario)
            result["payment_plan"] = plan
            store.record_event(conversation_id, kind="plan_built", channel=channel, detail=plan)
            if plan.get("mandate_links"):
                store.record_event(conversation_id, kind="mandate_issued", channel=channel,
                                   detail={"links": plan["mandate_links"]})

        _take_compose_failure()  # discard anything stale before this call
        reply_text = _compose_or_fallback(
            text, result["diagnosis"], scenario, decision, plan,
            conversation_context=transcript or None,
            outstanding_proposal=None if proposal is None else _describe_proposal(proposal),
        )
        compose_failure = _take_compose_failure()
        if compose_failure is not None:
            # The debtor still gets a known-safe sentence; what changes is
            # that the degradation is now visible instead of silent.
            result["composed"] = False
            result["compose_failure"] = compose_failure
            store.record_event(conversation_id, kind="compose_failed", channel=channel,
                               detail={"reason": compose_failure, "sent_instead": reply_text})
        else:
            result["composed"] = True

        try:
            send(reply_text)
        except ChannelUnavailable:
            _log.warning("inbound: reply send failed on %s", channel, exc_info=True)
            store.record_event(conversation_id, kind="reply_send_failed", channel=channel)
            return result

        result["agent_reply"] = reply_text
        store.record_turn(conversation_id, direction="outbound", text=reply_text)
        store.record_event(conversation_id, kind="agent_replied", channel=channel, detail={"text": reply_text})

        # A plan just put to them becomes the thing on the table, so the next
        # "yes" has something to attach to. Nothing is treated as agreed
        # here -- only as offered.
        if plan is not None:
            store.set_proposal(conversation_id, kind="payment_plan", detail=plan)

        return result
    finally:
        store.close()


@router.post("/telegram-webhook")
async def telegram_webhook(request: Request) -> dict[str, object]:
    """Telegram pushes a debtor's reply here the moment they send it.

    This replaces polling, and it is the difference between a demo that
    answers and one that only answers while a browser tab happens to be
    watching. Before this existed, a reply sent two minutes after the
    dashboard's polling window closed was simply never handled -- the
    debtor got silence, which is the single behaviour this project argues
    hardest against.

    It is also most of the latency. Polling cost up to a full interval
    before detection even began; a push costs nothing.

    Authenticated by Telegram's own `secret_token`, which it echoes in
    `X-Telegram-Bot-Api-Secret-Token` on every delivery. This endpoint is
    public, so an unauthenticated caller could otherwise fabricate a reply
    and make the system answer a message the debtor never sent -- the same
    class of risk `verify_and_ingest()` exists to close for Razorpay, and
    handled the same way: verify before doing anything with the body.

    Always returns 200. Telegram retries a non-2xx delivery, and a retry of
    a message that failed for a non-transient reason (an unparseable
    payload, a message from someone who isn't the demo contact) would just
    fail again forever. Genuine duplicates are stopped by the claim in
    `handle_inbound_message`, not by making Telegram give up.
    """
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if not expected:
        raise HTTPException(status_code=503, detail="TELEGRAM_WEBHOOK_SECRET not configured on this server")
    if request.headers.get("x-telegram-bot-api-secret-token") != expected:
        raise HTTPException(status_code=403, detail="bad or missing Telegram secret token")

    try:
        update = await request.json()
    except ValueError:
        return {"ok": True, "handled": False, "reason": "unparseable_body"}

    message = update.get("message") or update.get("edited_message") or {}
    text = message.get("text")
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if not text or not chat_id:
        # Photos, stickers, delivery receipts, join events. Acknowledged and
        # ignored rather than treated as a debtor reply.
        return {"ok": True, "handled": False, "reason": "no_text"}

    # Only the configured demo contact. A stranger who finds the bot must
    # never be able to drive the conversation, and their message must never
    # surface as though the demo's own debtor had sent it.
    demo_contact = os.environ.get("DEMO_CONTACT_TELEGRAM_CHAT_ID")
    # Fail closed. `if configured and ...` skipped the check entirely when
    # the variable was unset, so an unconfigured deployment accepted a
    # message from any chat, ran a real model call on it, and replied --
    # on a public endpoint. Caught by a probe from chat id "1" coming back
    # `handled: true` instead of `not_the_demo_contact`.
    #
    # There is no legitimate case for this endpoint talking to an unknown
    # chat, so an unset contact refuses rather than opening up.
    if not demo_contact:
        _log.warning("telegram webhook: DEMO_CONTACT_TELEGRAM_CHAT_ID is not configured -- refusing")
        return {"ok": True, "handled": False, "reason": "demo_contact_not_configured"}
    if chat_id != str(demo_contact):
        _log.info("telegram webhook: ignoring a message from a non-demo chat")
        return {"ok": True, "handled": False, "reason": "not_the_demo_contact"}

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise HTTPException(status_code=503, detail="TELEGRAM_BOT_TOKEN not configured on this server")

    channel_obj = TelegramChannel(token)
    try:
        result = handle_inbound_message(
            conversation_id=chat_id,
            external_id=str(update.get("update_id")),
            text=text,
            channel="telegram",
            send=lambda reply: channel_obj.send(to=chat_id, text=reply),
        )
    finally:
        channel_obj.close()

    return {"ok": True, **result}


# --------------------------------------------------------------------------
# The timeline: what actually happened, not what one browser tab witnessed.
# --------------------------------------------------------------------------

_STAGE_RANK = {
    "not_started": 0,
    "contacted": 1,
    "in_conversation": 2,
    "negotiating": 3,
    "mandate_issued": 4,
}

_STAGE_FOR_EVENT = {
    "message_sent": "contacted",
    "call_placed": "contacted",
    "reply_received": "in_conversation",
    "diagnosed": "in_conversation",
    "agent_replied": "in_conversation",
    "plan_built": "negotiating",
    "mandate_issued": "mandate_issued",
}

STAGE_LABEL = {
    "not_started": "Not started -- nothing has been sent yet",
    "contacted": "Contacted -- waiting on a reply",
    "in_conversation": "In conversation -- replies are being read and answered",
    "negotiating": "Negotiating -- a dated instalment plan is on the table",
    "mandate_issued": "Mandate issued -- a real e-mandate link is theirs to authorize",
    "escalated_to_human": "Escalated -- automated contact is paused, a person has it",
    "disputed_paused": "Disputed -- frozen, automated chasing has stopped",
}


def _stage_from_events(events) -> str:
    """How far this conversation has actually got.

    Furthest-reached rather than most-recent, because progress here isn't
    reversible by a later event: a mandate that has been issued stays
    issued even if the debtor's next message is small talk.

    The two exceptions are the ones that *should* override progress. A
    dispute freezes the account and an escalation hands it to a person --
    both mean automated contact has stopped, and reporting "negotiating"
    over the top of either would misstate what the system is doing. They
    are read from the latest events only, so an escalation that a later
    exchange moved past doesn't pin the stage forever.
    """
    stage = "not_started"
    for event in events:
        candidate = _STAGE_FOR_EVENT.get(event.kind)
        if candidate and _STAGE_RANK[candidate] > _STAGE_RANK[stage]:
            stage = candidate

    for event in reversed(events):
        if event.kind == "decided":
            detail = event.detail
            if detail.get("escalated_to_human") or detail.get("action") == "escalate_human":
                return "escalated_to_human"
            break
        if event.kind == "diagnosed" and event.detail.get("family") == "D":
            return "disputed_paused"
    return stage


@router.get("/timeline")
def demo_timeline(conversation_id: str | None = None, limit: int = 60) -> dict[str, object]:
    """Everything that happened, in order, with the conversation's stage.

    Unauthenticated on purpose, and read-only: it exposes the demo's own
    scripted invoice and the demo owner's own replies to their own bot,
    nothing belonging to a third party. Requiring the trigger secret here
    would mean baking it into a page that only wants to *watch*, which is a
    worse trade than publishing a demo transcript.

    This is what makes the dashboard honest. Its live console only ever
    showed events its own tab had seen happen, so a call placed before the
    page loaded, or a reply the Telegram webhook answered while nothing was
    polling, was invisible -- the system did the work and the UI showed an
    empty list.
    """
    limit = max(1, min(int(limit), 200))
    store = _conversation_store()
    try:
        events = store.recent_events(conversation_id=conversation_id, limit=limit)
        stage = _stage_from_events(events)
        proposal = store.outstanding_proposal(conversation_id) if conversation_id else None
        turns = store.recent_turns(conversation_id, limit=20) if conversation_id else []
    finally:
        store.close()

    return {
        "stage": stage,
        "stage_label": STAGE_LABEL[stage],
        "events": [
            {
                "at": e.at, "kind": e.kind, "channel": e.channel,
                "conversation_id": e.conversation_id, "detail": e.detail,
            }
            for e in events
        ],
        "turns": [{"direction": t.direction, "text": t.text, "at": t.at} for t in turns],
        "outstanding_proposal": None if proposal is None else {
            "kind": proposal.kind, "detail": proposal.detail, "proposed_at": proposal.proposed_at,
        },
    }


class DemoResetRequest(BaseModel):
    secret: str
    clear_conversation: bool = False
    """Wipe the transcript and timeline as well as the invoices.

    Off by default, because the timeline is the record of what this system
    actually did and deleting it is a bigger decision than putting an
    invoice back. A demo about to be recorded wants it; a debugging session
    almost never does.
    """


@router.post("/reset")
def reset_demo(payload: DemoResetRequest) -> dict[str, object]:
    """Put the demo back to its declared starting state.

    Rehearsing leaves real marks -- disputes raised, mandates scheduled,
    invoices out of the outstanding total -- and a recording that opens on
    last night's leftovers is a worse problem than this endpoint is a risk.

    Secret-gated like /demo/trigger, and narrow in the same way: it restores
    the invoices `agent/debtor/seed.py` declares and nothing else. A row
    created by something other than seeding is left alone, because "reset"
    should mean "back to the fixture", not "delete what I do not recognise".

    What it deliberately does **not** touch: the recovery ledger, the
    hash-chained ledger, and the promise history behind a debtor's score.
    Those record things that really happened -- a real capture, a real
    action, a real kept or broken promise -- and a demo convenience has no
    business rewriting them. A reset invoice with a real payment already
    attributed to it stays honest that way: the invoice is outstanding
    again, and the ledger still says the money moved.
    """
    _require_secret(payload.secret)

    store = InvoiceStore(os.environ.get("TRUECOMMIT_DEBTORS_DB", "debtors.db"))
    try:
        restored = reset_invoices(store)
    finally:
        store.close()

    cleared = False
    if payload.clear_conversation:
        conversation = _conversation_store()
        try:
            for channel in ("telegram", "ivr", "whatsapp"):
                conversation.clear(_conversation_id_for(channel))
            cleared = True
        finally:
            conversation.close()

    # In-process state too, or the first message after a reset hits a rate
    # limit from before it and the demo opens on a 429.
    _conversation_touches.clear()
    _last_triggered_at.clear()
    _last_triggered_at_by_number.clear()
    _mandate_link_cache.clear()
    global _last_payment_link_url, _last_followed_up_update_id
    _last_payment_link_url = None
    _last_followed_up_update_id = 0

    _log.info("demo: reset -- %d invoices restored, conversation cleared=%s", restored, cleared)
    return {
        "ok": True,
        "invoices_restored": restored,
        "conversation_cleared": cleared,
        "note": ("Ledgers and promise history are untouched -- those record things that "
                 "really happened."),
    }


# --------------------------------------------------------------------------
# The admin view: who owes what, what their record earns them, and why.
# --------------------------------------------------------------------------

def _debtor_dict(debtor, terms) -> dict[str, object]:
    return {
        "id": debtor.id,
        "display_name": debtor.display_name,
        "channel": debtor.channel,
        "channel_ref": debtor.channel_ref,
        "invoice_id": debtor.invoice_id,
        "invoice_amount_paise": debtor.invoice_amount_paise,
        # Never omitted. A declared history is a fixture for showing what
        # the scoring does across its range; letting it read as evidence of
        # real behaviour would be the same overclaim docs/RESULTS.md
        # already refuses to make about simulated recovery.
        "is_seeded": debtor.is_seeded,
        "note": debtor.note,
        "score": {
            "band": terms.band,
            "credibility": terms.credibility,
            "credibility_pct": terms.credibility_pct,
            "kept": terms.kept_promises,
            "resolved": terms.resolved_promises,
            "rationale": terms.rationale,
        },
        "terms": {
            "grace_days": terms.grace_days,
            "max_instalments": terms.max_instalments,
            "offers_instalment_plan": terms.offers_instalment_plan,
            "early_discount_rate": terms.early_discount_rate,
            "press_statutory_interest": terms.press_statutory_interest,
        },
    }


@router.get("/debtors")
def list_debtors() -> dict[str, object]:
    """Every debtor on the register with the terms their record earns.

    Read-only and unauthenticated, like /demo/timeline: it exposes seeded
    fixtures plus the demo owner's own record, nothing belonging to a third
    party.
    """
    registry = _registry()
    try:
        debtors = registry.all_debtors()
        rows = []
        for debtor in debtors:
            # Time passing is what breaks a promise. Resolving before
            # scoring stops a stale 'pending' flattering anyone.
            registry.expire_overdue_promises(debtor.id)
            rows.append(_debtor_dict(debtor, registry.terms(debtor.id)))
    finally:
        registry.close()
    return {"debtors": rows, "bands": [
        {"min_credibility": minimum, "band": band, "grace_days": grace,
         "max_instalments": instalments, "early_discount_rate": discount,
         "press_statutory_interest": press}
        for minimum, band, grace, instalments, discount, press in BANDS
    ]}


@router.get("/debtors/{debtor_id}")
def debtor_detail(debtor_id: str) -> dict[str, object]:
    """One debtor: their score, every promise behind it, and the
    conversation that produced them -- the whole basis for the terms they
    are being offered, in one place."""
    registry = _registry()
    try:
        debtor = registry.debtor(debtor_id)
        if debtor is None:
            raise HTTPException(status_code=404, detail=f"no debtor {debtor_id!r}")
        registry.expire_overdue_promises(debtor.id)
        terms = registry.terms(debtor.id)
        promises = [
            {"invoice_id": o.invoice_id, "amount_paise": o.promised_amount_paise,
             "promised_date": o.promised_date.isoformat(), "outcome": o.outcome,
             "payment_id": o.payment_id, "recorded_at": o.recorded_at}
            for o in registry.outcomes_for(debtor.id)
        ]
        detail = _debtor_dict(debtor, terms)
    finally:
        registry.close()

    store = _conversation_store()
    try:
        turns = [{"direction": t.direction, "text": t.text, "at": t.at}
                 for t in store.recent_turns(debtor.channel_ref, limit=40)]
        events = [{"at": e.at, "kind": e.kind, "channel": e.channel, "detail": e.detail}
                  for e in store.recent_events(conversation_id=debtor.channel_ref, limit=60)]
    finally:
        store.close()

    detail["promises"] = promises
    detail["turns"] = turns
    detail["events"] = events
    return detail


# --------------------------------------------------------------------------
# The subscription side: a debit that will fail, said before it fails.
# --------------------------------------------------------------------------

def _portfolio() -> MandatePortfolio:
    return MandatePortfolio(os.environ.get("TRUECOMMIT_DEBTORS_DB", "debtors.db"))


SUBSCRIPTION_THREAD_PREFIX = "sub:"
"""Namespaces the subscription conversation.

Telegram's private-chat id is the *user's* id, not a per-bot one -- so the
b2b bot and the subscription bot both report the same chat id for the same
person. Keying the conversation on that alone would have merged the two
threads in the store: one transcript, one outstanding proposal, and a plan
offered by one bot acceptable to the other. Two separate bots would have
looked separate in Telegram and been a single conversation underneath,
which is worse than not splitting them at all.

Found when the second bot's chat id came back identical to the first's."""


def _subscription_conversation_id(chat_id: str | None = None) -> str:
    """The subscription bot's own thread, namespaced away from b2b's."""
    raw = chat_id or os.environ.get("DEMO_CONTACT_SUBSCRIPTION_CHAT_ID") or "subscription"
    return f"{SUBSCRIPTION_THREAD_PREFIX}{raw}"


def _subscription_channel() -> TelegramChannel:
    token = os.environ.get("TELEGRAM_SUBSCRIPTION_BOT_TOKEN")
    if not token:
        raise HTTPException(status_code=503,
                            detail="TELEGRAM_SUBSCRIPTION_BOT_TOKEN not configured on this server")
    return TelegramChannel(token)


@router.get("/mandate-health")
def mandate_health() -> dict[str, object]:
    """Run the real detector across the mandate book.

    `check_mandate_health()` has existed and been tested since early on, and
    was reachable from no endpoint at all -- an offline tool ran it once and
    wrote a markdown file. That is the wrong thing to leave unreachable,
    because it is the strongest claim in the project: detection here is
    arithmetic on each mandate's own fields, not a prediction.

    Read-only and unauthenticated, like the other demo reads.
    """
    portfolio = _portfolio()
    try:
        return scan(portfolio.all())
    finally:
        portfolio.close()


class SubscriptionAlertRequest(BaseModel):
    secret: str
    failure: str = "headroom"
    """Which defect to demonstrate. The six are genuinely different failures
    with different repairs, so the caller picks one rather than getting
    whichever happens to be first."""
    to: str | None = None
    """A phone number for the voice call -- so someone trying this can have
    it ring their own phone. Same E.164 validation and per-number cooldown
    as /demo/trigger."""
    call: bool = True


def _defect_explanation(defect_value: str, m) -> str:
    """Why this debit will fail, in the debtor's terms rather than the
    detector's field names.

    The arithmetic still travels with it -- a claim like "this will fail" is
    worth much more when the reader can check it themselves.
    """
    rupees = lambda p: to_rupees_display(p)  # noqa: E731
    return {
        "HEADROOM_BREACH": (
            f"the mandate you authorized has a ceiling of {rupees(m.max_amount_paise)}, "
            f"and the next debit is {rupees(m.upcoming_debit_paise)}. The bank will refuse "
            f"it -- not for want of funds, but because the authorization itself is too small."
        ),
        "EXPIRY_BEFORE_DEBIT": (
            f"the mandate expires on {str(m.end_at)[:10]}, which is before the next debit on "
            f"{str(m.next_debit_date)[:10]}. There will be no live authorization left to charge."
        ),
        "AFA_THRESHOLD_BREACH": (
            f"the next debit is {rupees(m.upcoming_debit_paise)}, above the Rs 15,000 ceiling "
            f"that RBI allows without you authenticating it. No authentication step is "
            f"scheduled, so the debit will be declined."
        ),
        "REPEAT_NSF": (
            f"the last {m.consecutive_nsf} attempts were returned for insufficient funds. "
            f"Presenting a {rupees(m.upcoming_debit_paise)} debit on the same date is likely "
            f"to be returned again."
        ),
        "SILENT_REVOCATION": (
            "this mandate is no longer active at your bank, and the last cycle was never even "
            "attempted. Nothing will be collected until it is set up again."
        ),
        "RAIL_DEGRADED": (
            f"your bank is currently returning an elevated share of mandate debits "
            f"({m.issuer_failure_rate:.0%}). This debit is more likely than usual to be "
            f"returned for reasons that have nothing to do with your account."
        ),
    }.get(defect_value, "a defect was detected on this mandate.")


def _corrected_mandate_link(m, defects) -> tuple[str | None, int]:
    """A real, authorizable Razorpay mandate at an amount that actually works.

    This is the honest form of "change your bank". Razorpay has no API to
    swap the account behind an existing subscription -- but a fresh
    authorization link lets the debtor pick whichever account they like on
    Razorpay's own hosted page, and the old mandate is revoked once the new
    one is live. A new link *is* the bank change, done the only way the rail
    allows.

    The corrected amount is the upcoming debit with headroom above it, so the
    replacement does not reproduce the defect it exists to fix.
    """
    corrected = int(m.upcoming_debit_paise * 1.2)
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        return None, corrected

    try:
        rail = RazorpayRail(key_id=key_id, key_secret=key_secret)
        mandate = rail.create_mandate(MandateSpec(
            max_amount_paise=corrected,
            start_at=str(m.next_debit_date),
            end_at=str(m.end_at),
            debit_schedule=[str(m.next_debit_date)[:10]],
            afa_required=corrected > 15_000_00,
        ))
        return mandate.short_url, corrected
    except Exception:
        # Never a placeholder URL. An unavailable link means the message
        # says so -- this project shipped a fake one once.
        _log.warning("subscription: could not create the corrected mandate", exc_info=True)
        return None, corrected


@router.post("/subscription-alert")
def subscription_alert(payload: SubscriptionAlertRequest) -> dict[str, object]:
    """Warn a subscriber that their next debit will fail, before it does.

    The whole argument of this endpoint is the tense. Every dunning system
    messages someone *after* a failed payment. This one runs a deterministic
    check over the mandate's own fields, finds a debit that cannot succeed,
    and says so while there is still time to fix it -- with the reason, the
    arithmetic, and a working replacement.

    And the message is not an extra: RBI_EMANDATE_PREDEBIT_24H requires a
    pre-debit notice carrying five specific fields. `predebit_notice.py`
    builds exactly those. So this is the compliant notification, finally
    carrying something useful.

    Runs through the same `check_bounds()` gate as every other outbound
    contact -- a warning is still a contact.
    """
    _require_secret(payload.secret)

    mandate_id = FAILURE_KINDS.get(payload.failure)
    if mandate_id is None:
        raise HTTPException(status_code=400, detail=(
            f"unknown failure kind {payload.failure!r} -- one of {sorted(FAILURE_KINDS)}"))

    portfolio = _portfolio()
    try:
        target = next((m for m in portfolio.all() if m.mandate_id == mandate_id), None)
    finally:
        portfolio.close()
    if target is None:
        raise HTTPException(status_code=503, detail="the mandate portfolio has not been seeded")

    # The real detector, on this mandate, right now.
    defects = check_mandate_health(target.to_health_input())
    if not defects:
        raise HTTPException(status_code=409, detail=(
            f"{mandate_id} is healthy -- nothing to warn about. It may already have been repaired."))

    _check_rate_limit("subscription")

    scenario = {"invoice_id": target.mandate_id, "amount_paise": target.upcoming_debit_paise,
                "days_overdue": 0}
    bounds_result = check_bounds(_bounds_context_for(scenario, "telegram", replying_to_inbound=False))
    if not bounds_result.passed:
        raise HTTPException(status_code=422, detail=(
            f"check_bounds() refused this alert: {[v.rule_id for v in bounds_result.refusals]}"))

    # business_now(), not datetime.now(): the portfolio stores tz-aware
    # timestamps, and the debtor's "in 4 days" is counted on their
    # calendar rather than the server's (agent/clock.py, WHAT_BROKE #20).
    days = max(0, (datetime.fromisoformat(target.next_debit_date) - business_now()).days)
    link, corrected = _corrected_mandate_link(target, defects)
    primary = defects[0]

    lines = [
        f"Heads up -- your next subscription debit will fail.",
        "",
        f"{target.plan} · {to_rupees_display(target.upcoming_debit_paise)} due "
        f"{str(target.next_debit_date)[:10]}"
        + (f" (in {days} days)" if days else " (today)"),
        "",
        f"Why: {_defect_explanation(primary.defect.value, target)}",
        "",
        f"We found this before presenting the debit, so nothing has been declined and no "
        f"failed-payment fee applies.",
    ]
    if link:
        lines += [
            "",
            f"Fix it here: {link}",
            f"That authorizes a replacement mandate at {to_rupees_display(corrected)} -- and you "
            f"can pick a different bank account on that page if you'd rather. The old one is "
            f"cancelled once this is live. Nothing is charged now.",
        ]
    else:
        lines += ["", "A person will be in touch with a working link shortly -- nothing has "
                        "been charged."]

    text = "\n".join(lines)

    raw_chat = os.environ.get("DEMO_CONTACT_SUBSCRIPTION_CHAT_ID", "")
    chat_id = _subscription_conversation_id()
    channel_obj = _subscription_channel()
    try:
        result = channel_obj.send(to=raw_chat or chat_id, text=text)
    except ChannelUnavailable as exc:
        raise HTTPException(status_code=502, detail=f"channel unavailable: {exc}") from exc
    finally:
        channel_obj.close()

    store = _conversation_store()
    try:
        store.record_turn(chat_id, direction="outbound", text=text)
        store.record_event(chat_id, kind="failure_predicted", channel="telegram", detail={
            "mandate_id": target.mandate_id, "customer": target.customer,
            "defect": primary.defect.value, "repair": primary.repair, "detail": primary.detail,
            "upcoming_debit_paise": target.upcoming_debit_paise,
            "max_amount_paise": target.max_amount_paise,
            "days_until_debit": days, "corrected_amount_paise": corrected,
            "fix_url": link, "text": text,
            "bounds_passed": len([v for v in bounds_result.verdicts if v.verdict == "PASS"]),
            "bounds_total": len(bounds_result.verdicts),
        })
    finally:
        store.close()

    call_ref = None
    if payload.call:
        call_ref = _place_subscription_call(target, primary, days, payload.to)

    return {
        "status": result.status,
        "mandate_id": target.mandate_id,
        "customer": target.customer,
        "defect": primary.defect.value,
        "why": primary.detail,
        "repair": primary.repair,
        "days_until_debit": days,
        "fix_url": link,
        "corrected_amount_paise": corrected,
        "call_ref": call_ref,
        "bounds_checks": [v.rule_id for v in bounds_result.verdicts if v.verdict == "PASS"],
        "bounds_total": len(bounds_result.verdicts),
        "telegram_text": text,
    }


def _place_subscription_call(target, defect, days: int, to: str | None) -> str | None:
    """A voice call that says only enough to get them to read the message.

    Deliberately short and free of numbers: a spoken rupee amount and a
    mandate reference are exactly what a person cannot write down mid-call,
    and the detail is already sitting in Telegram where they can act on it.
    """
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    from_number = os.environ.get("TWILIO_FROM_NUMBER")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    api_key_secret = os.environ.get("TWILIO_API_KEY_SECRET")
    if not sid or not from_number or not (auth_token or api_key_secret):
        _log.info("subscription: no Twilio config -- skipping the call")
        return None

    try:
        phone = _resolve_recipient(to, "DEMO_CONTACT_PHONE_NUMBER")
    except HTTPException:
        raise
    secret_value, username = ((api_key_secret, os.environ.get("TWILIO_API_KEY_SID"))
                              if api_key_secret else (auth_token, None))
    when = f"in {days} days" if days else "today"
    text = (
        "Hello, this is an automated call from True Commit about your subscription. "
        f"Your next automatic payment, due {when}, will not go through. "
        "This is not a missed payment, and nothing has been charged. "
        "Please check Telegram, where we have sent the reason and a link to fix it. Thank you."
    )
    channel_obj = TwilioVoiceChannel(sid, secret_value, from_number, auth_username=username)
    try:
        return channel_obj.send(to=phone, text=text).external_ref
    except ChannelUnavailable:
        _log.warning("subscription: the call could not be placed", exc_info=True)
        return None
    finally:
        channel_obj.close()


@router.post("/telegram-webhook/subscription")
async def subscription_telegram_webhook(request: Request) -> dict[str, object]:
    """The subscription bot's own inbound path.

    A separate route rather than a shared one with a bot discriminator: when
    a delivery starts failing, the URL in `getWebhookInfo` says immediately
    which bot broke. A shared endpoint that 403s tells you only that
    *something* is misconfigured, and this project has already lost an
    evening to exactly that ambiguity.

    Everything downstream is the same machinery the b2b bot uses --
    `handle_inbound_message` runs the real extractor, the real
    DECIDE -> BOUNDS decision and the real composer. The subscription demo
    is not a second, simpler pipeline; it is the same one, pointed at a
    different conversation.
    """
    expected = os.environ.get("TELEGRAM_SUBSCRIPTION_WEBHOOK_SECRET")
    if not expected:
        raise HTTPException(status_code=503,
                            detail="TELEGRAM_SUBSCRIPTION_WEBHOOK_SECRET not configured")
    if request.headers.get("x-telegram-bot-api-secret-token") != expected:
        raise HTTPException(status_code=403, detail="bad or missing Telegram secret token")

    try:
        update = await request.json()
    except ValueError:
        return {"ok": True, "handled": False, "reason": "unparseable_body"}

    message = update.get("message") or update.get("edited_message") or {}
    text = message.get("text")
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if not text or not chat_id:
        return {"ok": True, "handled": False, "reason": "no_text"}

    configured = os.environ.get("DEMO_CONTACT_SUBSCRIPTION_CHAT_ID")
    # Fail closed. `if configured and ...` skipped the check entirely when
    # the variable was unset, so an unconfigured deployment accepted a
    # message from any chat, ran a real model call on it, and replied --
    # on a public endpoint. Caught by a probe from chat id "1" coming back
    # `handled: true` instead of `not_the_demo_contact`.
    #
    # There is no legitimate case for this endpoint talking to an unknown
    # chat, so an unset contact refuses rather than opening up.
    if not configured:
        _log.warning("subscription webhook: DEMO_CONTACT_SUBSCRIPTION_CHAT_ID is not configured -- refusing")
        return {"ok": True, "handled": False, "reason": "demo_contact_not_configured"}
    if chat_id != str(configured):
        _log.info("subscription webhook: ignoring a message from a non-demo chat")
        return {"ok": True, "handled": False, "reason": "not_the_demo_contact"}

    channel_obj = _subscription_channel()
    try:
        result = handle_inbound_message(
            # Namespaced for the store, raw for the send: the thread must
            # not collide with b2b's, and Telegram still needs the real id.
            conversation_id=_subscription_conversation_id(chat_id),
            external_id=str(update.get("update_id")),
            text=text, channel="telegram", scenario_key="subscription",
            send=lambda reply: channel_obj.send(to=chat_id, text=reply),
        )
    finally:
        channel_obj.close()

    return {"ok": True, **result}
