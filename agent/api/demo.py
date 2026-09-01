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
from agent.debtor.registry import DebtorRegistry
from agent.debtor.score import BANDS, DebtorTerms, terms_for
from agent.mandate.emandate import create_plan_mandates, describe_mandate_links
from agent.mandate.payment_plan import PlanRejected, build_plan, describe_plan
from agent.notify.conversation import ConversationStore
from agent.notify.compose import ComposeFailed, compose_reply
from agent.notify.protocol import ChannelUnavailable
from agent.notify.telegram import TelegramChannel
from agent.notify.twilio_voice import TwilioVoiceChannel
from agent.notify.twilio_whatsapp import TwilioWhatsAppChannel
from agent.rails.razorpay_rail import RazorpayRail
from agent.rails.types import InvoiceSpec, LinkSpec

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
                      terms: DebtorTerms | None = None) -> dict[str, object] | None:
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

    total = int(scenario["amount_paise"])
    try:
        first_date = date.fromisoformat(promise.date)
    except ValueError:  # pragma: no cover -- ExtractionResult validates ISO8601 upstream
        return None

    terms = terms or terms_for([])

    # What the debtor actually said, if they said more than one thing.
    stated_legs = _legs_from_schedule(promise, total)
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
            debtor = registry.by_channel_ref(conversation_id)
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

        plan = _plan_from_promise(extraction, scenario, terms)
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
    if demo_contact and chat_id != str(demo_contact):
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
