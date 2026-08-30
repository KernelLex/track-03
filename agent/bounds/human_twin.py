"""The differential test's other half (§13.4). Each function here was written by
reading its rule's `human:` prose in rules.yaml — not derived from `machine:` by
transformation, which would make agreement between them tautological rather
than evidence of anything.

§13.4's own caveat applies in full: agreement between this module and
engine.check_bounds() demonstrates *implementation consistency* — two
readings of the same intent agree — not correctness against the actual
regulation. The same person wrote both. That's stated in LIMITATIONS.md,
not just here.
"""

from __future__ import annotations

from datetime import time, timedelta
from typing import Callable

from agent.bounds.context import ALL_CHANNELS, REQUIRED_PREDEBIT_FIELDS, BoundsContext


def rbi_emandate_predebit_24h(ctx: BoundsContext) -> bool:
    if not ctx.action.presents_mandate_debit:
        return True
    notified = ctx.mandate.last_notification_at
    if notified is None:
        return False
    if ctx.now - notified < timedelta(days=1):
        return False
    return REQUIRED_PREDEBIT_FIELDS.issubset(ctx.notification.fields)


def rbi_emandate_afa_ceiling(ctx: BoundsContext) -> bool:
    if ctx.debit_paise <= 1_500_000:
        return True
    return ctx.action.afa_reference is not None


def rbi_emandate_postdebit(ctx: BoundsContext) -> bool:
    if not ctx.action.presents_mandate_debit:
        return True
    return ctx.post_debit_notification_queued is True


def rbi_emandate_optout(ctx: BoundsContext) -> bool:
    if ctx.debtor.opted_out_cycle:
        return False
    if ctx.mandate.status == "revoked":
        return False
    return True


def rbi_fpc_hours(ctx: BoundsContext) -> bool:
    lo, hi = time(8, 0), time(19, 0)
    return lo <= ctx.debtor.local_time < hi


def trai_dnd(ctx: BoundsContext) -> bool:
    channel = ctx.action.channel
    if channel is None:
        return True
    return channel not in ctx.debtor.opted_out_channels


def msmed_interest_basis(ctx: BoundsContext) -> bool:
    if ctx.action.type != "send_statutory_notice":
        return True
    if ctx.interest_computed_from != ctx.config.rbi_bank_rate:
        return False
    return ctx.config.as_of_age_days <= 120


def touch_budget(ctx: BoundsContext) -> bool:
    if ctx.action.is_regulatory_notice:
        return True
    return ctx.debtor.touches_7d < 3


def dispute_freeze(ctx: BoundsContext) -> bool:
    if ctx.debtor.state != "DISPUTED_FROZEN":
        return True
    return ctx.action.type in ("escalate_human", "no_action")


def attempt_ceiling(ctx: BoundsContext) -> bool:
    return ctx.invoice.recovery_attempts < 6


def ev_floor(ctx: BoundsContext) -> bool:
    return ctx.decision.ev_paise > 0


def promise_cooldown(ctx: BoundsContext) -> bool:
    if ctx.debtor.state != "PROMISED":
        return True
    if ctx.promise_date is None:
        return False
    effective_grace = ctx.config.grace_days * ctx.debtor.promise_credibility
    return ctx.now >= ctx.promise_date + timedelta(days=effective_grace)


def exhausted(ctx: BoundsContext) -> bool:
    return ctx.debtor.state != "EXHAUSTED"


def mandate_param_clamp(ctx: BoundsContext) -> bool:
    if ctx.action.type != "create_mandate":
        return True
    if ctx.action.params == ctx.action.debtor_stated_params:
        return True
    if ctx.action.clamp_direction == "favours_debtor":
        return True
    return ctx.action.human_approval_id is not None


def no_mandate_on_dispute(ctx: BoundsContext) -> bool:
    if ctx.action.type != "create_mandate":
        return True
    return ctx.invoice.disputed_paise == 0


def statutory_human_gate(ctx: BoundsContext) -> bool:
    if not ctx.action.carries_legal_number:
        return True
    return ctx.action.human_approval_id is not None


def rail_disclosure(ctx: BoundsContext) -> bool:
    return ctx.action.rail_tag is not None


def channel_exhaustion(ctx: BoundsContext) -> bool:
    if len(ctx.debtor.opted_out_channels) < len(ALL_CHANNELS):
        return True
    if ctx.action.type in ("escalate_human", "no_action"):
        return True
    return ctx.action.is_regulatory_notice is True


REGISTRY: dict[str, Callable[[BoundsContext], bool]] = {
    "RBI_EMANDATE_PREDEBIT_24H": rbi_emandate_predebit_24h,
    "RBI_EMANDATE_AFA_CEILING": rbi_emandate_afa_ceiling,
    "RBI_EMANDATE_POSTDEBIT": rbi_emandate_postdebit,
    "RBI_EMANDATE_OPTOUT": rbi_emandate_optout,
    "RBI_FPC_HOURS": rbi_fpc_hours,
    "TRAI_DND": trai_dnd,
    "MSMED_INTEREST_BASIS": msmed_interest_basis,
    "TOUCH_BUDGET": touch_budget,
    "DISPUTE_FREEZE": dispute_freeze,
    "ATTEMPT_CEILING": attempt_ceiling,
    "EV_FLOOR": ev_floor,
    "PROMISE_COOLDOWN": promise_cooldown,
    "EXHAUSTED": exhausted,
    "MANDATE_PARAM_CLAMP": mandate_param_clamp,
    "NO_MANDATE_ON_DISPUTE": no_mandate_on_dispute,
    "STATUTORY_HUMAN_GATE": statutory_human_gate,
    "RAIL_DISCLOSURE": rail_disclosure,
    "CHANNEL_EXHAUSTION": channel_exhaustion,
}
