"""Typed actions, each named with its Razorpay object. DEVDOC_v6 §11.5.

This module is descriptive metadata about the action set — which Razorpay
object each action touches, which are human-gated, which can move money.
It is not itself an enforcement mechanism: STATUTORY_HUMAN_GATE and the
other check_bounds() rules (agent/bounds/) enforce the real constraints
independently, so bypassing this module can't bypass them.
"""

from __future__ import annotations

from enum import Enum


class ActionType(str, Enum):
    REISSUE_ARTIFACT = "reissue_artifact"
    CREATE_PAYMENT_LINK = "create_payment_link"
    REQUEST_RECONCILIATION = "request_reconciliation"
    SEND_REMINDER = "send_reminder"
    SEND_PREDEBIT_NOTICE = "send_predebit_notice"
    SEND_POSTDEBIT_NOTICE = "send_postdebit_notice"
    CHECK_MANDATE_HEALTH = "check_mandate_health"
    CREATE_MANDATE = "create_mandate"
    REPAIR_MANDATE = "repair_mandate"
    RETRY_CHARGE = "retry_charge"
    SEND_STATUTORY_NOTICE = "send_statutory_notice"
    INITIATE_REFUND = "initiate_refund"
    REVOKE_MANDATE = "revoke_mandate"
    ESCALATE_HUMAN = "escalate_human"
    NO_ACTION = "no_action"


RAZORPAY_OBJECT_FOR_ACTION: dict[ActionType, str | None] = {
    ActionType.REISSUE_ARTIFACT: "invoice (inv_*)",
    ActionType.CREATE_PAYMENT_LINK: "payment_link (plink_*)",
    ActionType.REQUEST_RECONCILIATION: None,
    ActionType.SEND_REMINDER: None,
    ActionType.SEND_PREDEBIT_NOTICE: None,
    ActionType.SEND_POSTDEBIT_NOTICE: None,
    ActionType.CHECK_MANDATE_HEALTH: None,
    ActionType.CREATE_MANDATE: "subscription/token",
    ActionType.REPAIR_MANDATE: "subscription update",
    ActionType.RETRY_CHARGE: "payment (pay_*)",
    ActionType.SEND_STATUTORY_NOTICE: None,
    ActionType.INITIATE_REFUND: "refund (rfnd_*)",
    ActionType.REVOKE_MANDATE: "subscription cancel",
    ActionType.ESCALATE_HUMAN: None,
    ActionType.NO_ACTION: None,
}

MONEY_MOVING_ACTIONS: frozenset[ActionType] = frozenset({
    ActionType.RETRY_CHARGE,
    ActionType.CREATE_MANDATE,
})
"""Actions that can themselves result in money moving. Law 9: every one of
these needs a named inverse in reversal.REVERSAL_MAP. create_payment_link and
reissue_artifact don't move money directly — the debtor's subsequent payment
does, attributed at SETTLE (Law 7), not at ACT — so they carry no reversal
obligation under Law 9 even though reissue_artifact still has one for a
different reason (§11.6): it's cheap and conspicuous by absence, not because
Law 9 strictly requires it."""

HUMAN_GATED_ACTIONS: frozenset[ActionType] = frozenset({
    ActionType.SEND_STATUTORY_NOTICE,
    ActionType.INITIATE_REFUND,
    ActionType.REVOKE_MANDATE,
})
"""§11.5's gate column, for documentation/UI purposes. The actual enforcement
is STATUTORY_HUMAN_GATE and friends in agent/bounds/rules.yaml — this set
cannot be the only thing standing between an action and execution."""
