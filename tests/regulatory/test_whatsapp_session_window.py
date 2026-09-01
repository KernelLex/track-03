"""WHATSAPP_SESSION_WINDOW — Meta's 24-hour customer-service window.

Named by the rule's own `test:` field in `agent/bounds/rules.yaml`.

**This one is a platform rule, not law**, which is why it lives in the
stopping register beside the limits this system imposes on itself rather
than in the regulatory register with RBI, TRAI and MSMED. Filing a vendor's
terms of service as regulation would be exactly the overclaim this project
criticises elsewhere.

It is empirically grounded rather than only read: a real send hit Twilio
error 63016 -- "failed to send freeform message because you are outside the
allowed window" -- during channel bring-up. The rule models a constraint
this project has actually been refused by.

Why a collections agent specifically needs it: the conversational reply
path and the cold-outreach path are structurally different actions, and an
agent that does not model the difference silently fails to deliver at
exactly the moment it believes it is chasing. A message the platform drops
is worse than one the gate refuses -- the gate's refusal is at least logged.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from agent.bounds.context import (
    ActionCtx,
    BoundsContext,
    ConfigCtx,
    DebtorCtx,
    DecisionCtx,
    InvoiceCtx,
    MandateCtx,
)
from agent.bounds.engine import check_bounds

NOW = datetime(2026, 9, 2, 12, 0)


def context(**overrides) -> BoundsContext:
    base = dict(
        debtor=DebtorCtx(id="debtor_1", state="ENGAGED"),
        mandate=MandateCtx(),
        action=ActionCtx(type="send_reminder", channel="whatsapp", rail_tag="simulated"),
        decision=DecisionCtx(ev_paise=1000),
        invoice=InvoiceCtx(id="inv_1"),
        config=ConfigCtx(),
        now=NOW,
    )
    base.update(overrides)
    return BoundsContext(**base)


def verdict_for(ctx: BoundsContext) -> str:
    return next(v.verdict for v in check_bounds(ctx).verdicts
                if v.rule_id == "WHATSAPP_SESSION_WINDOW")


def test_refuses_freeform_outside_the_window():
    """The case the rule exists for, and the one Twilio refused live."""
    assert verdict_for(context(last_inbound_at=NOW - timedelta(hours=31))) == "REFUSE"


def test_allows_freeform_inside_the_window():
    assert verdict_for(context(last_inbound_at=NOW - timedelta(hours=2))) == "PASS"


@pytest.mark.parametrize("hours,expected", [(23, "PASS"), (25, "REFUSE")])
def test_the_boundary_is_twenty_four_hours(hours, expected):
    assert verdict_for(context(last_inbound_at=NOW - timedelta(hours=hours))) == expected


def test_a_debtor_who_has_never_written_has_no_window_open():
    """Cold outreach. `last_inbound_at is None` is not "unknown, assume
    fine" -- it means they have never messaged us, which is precisely when
    a free-form send is not permitted."""
    assert verdict_for(context(last_inbound_at=None)) == "REFUSE"


def test_an_approved_template_is_allowed_outside_the_window():
    """The template path is the *only* legal way to open a conversation
    outside the window, so the rule must not block it -- otherwise cold
    outreach becomes impossible on WhatsApp entirely."""
    ctx = context(
        last_inbound_at=NOW - timedelta(days=9),
        action=ActionCtx(type="send_reminder", channel="whatsapp",
                         rail_tag="simulated", uses_approved_template=True),
    )
    assert verdict_for(ctx) == "PASS"


@pytest.mark.parametrize("channel", ["telegram", "sms", "email", "ivr", None])
def test_other_channels_are_unaffected(channel):
    """Meta's rule governs WhatsApp. A gate that quietly applied it to
    Telegram would be inventing a restriction no platform imposes."""
    ctx = context(
        last_inbound_at=None,
        action=ActionCtx(type="send_reminder", channel=channel, rail_tag="simulated"),
    )
    assert verdict_for(ctx) == "PASS"


@pytest.mark.parametrize("action_type", ["escalate_human", "no_action"])
def test_stopping_and_escalating_are_never_refused(action_type):
    """The WHAT_BROKE #12/#14 family: neither is a send to the debtor, so a
    send restriction has nothing to say about them. Refusing them here
    would mean an out-of-window WhatsApp case could be neither answered nor
    handed to a person -- silence, which is the outcome this project argues
    against most explicitly."""
    ctx = context(
        last_inbound_at=None,
        action=ActionCtx(type=action_type, channel="whatsapp", rail_tag="simulated"),
    )
    assert verdict_for(ctx) == "PASS"


def test_it_is_filed_as_a_stopping_rule_not_a_regulatory_one():
    """Meta's platform policy is not law. The registers mean different
    things -- `regulatory` carries `source` and `clause_ref` for statutes
    this system must not breach -- and putting a vendor's terms there would
    overstate what it is."""
    import yaml
    from pathlib import Path

    rules = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "agent" / "bounds" / "rules.yaml")
        .read_text(encoding="utf-8")
    )
    assert "WHATSAPP_SESSION_WINDOW" in {r["id"] for r in rules["stopping"]}
    assert "WHATSAPP_SESSION_WINDOW" not in {r["id"] for r in rules["regulatory"]}
