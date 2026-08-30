"""The fixed context every bounds rule is evaluated against. DEVDOC_v6 §13.1, §13.2.

Field names here are chosen to match the dotted paths used in DEVDOC_v6's
rule bodies (`debtor.touches_7d`, `action.type`, ...) as closely as valid
Python identifiers allow, so a rule string reads the same in the doc and in
rules.yaml. Where the doc's own pseudocode isn't valid Python (`IS NOT
NULL`, `TRUE`, `=>`, `⊇`), this module and rules.yaml translate it once,
consistently — see the header comment in rules.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time

ALL_CHANNELS: frozenset[str] = frozenset({"sms", "email", "whatsapp", "ivr"})


@dataclass
class DebtorCtx:
    id: str
    state: str = "HEALTHY"
    touches_7d: int = 0
    opted_out_cycle: bool = False
    opted_out_channels: frozenset[str] = field(default_factory=frozenset)
    local_time: time = time(12, 0)
    promise_credibility: float = 1.0
    """Precomputed upstream from kept/broken history (§24.2) — SYSTEM-derived,
    never assigned here from a model's read of sincerity. Defaults to 1.0
    (benefit of the doubt) for a debtor with no promise history yet."""


@dataclass
class MandateCtx:
    id: str | None = None
    status: str | None = None
    last_notification_at: datetime | None = None
    afa_required: bool = True


@dataclass
class NotificationCtx:
    fields: frozenset[str] = field(default_factory=frozenset)


REQUIRED_PREDEBIT_FIELDS: frozenset[str] = frozenset(
    {"merchant_name", "amount", "debit_datetime", "mandate_ref", "reason"}
)


@dataclass
class ActionCtx:
    type: str = "none"
    channel: str | None = None
    afa_reference: str | None = None
    human_approval_id: str | None = None
    carries_legal_number: bool = False
    rail_tag: str | None = None
    is_regulatory_notice: bool = False
    presents_mandate_debit: bool = False
    """True only when ACT is about to call rail.present_debit(...). Deliberately not
    inferred from type == 'retry_charge' alone — §11.5 overloads that one type across
    both a plain one-time retry and a mandate debit presentment, and only the latter
    needs the pre/post-debit notification gates (§13.1)."""
    params: dict[str, object] | None = None
    debtor_stated_params: dict[str, object] | None = None
    clamp_direction: str | None = None


@dataclass
class DecisionCtx:
    ev_paise: int = 0


@dataclass
class InvoiceCtx:
    id: str | None = None
    recovery_attempts: int = 0
    disputed_paise: int = 0


@dataclass
class ConfigCtx:
    promise_credibility_floor: float = 0.34
    """Not given a value in DEVDOC_v6 §24.2 beyond the name `floor` — a
    starting default, same spirit as the Auditor's 10% sampling default
    (§11.7): cheap to tune once real promise-kept data exists."""
    grace_days: int = 3
    rbi_bank_rate: float = 0.0550
    as_of_age_days: int = 0


@dataclass
class BoundsContext:
    debtor: DebtorCtx
    mandate: MandateCtx
    action: ActionCtx
    decision: DecisionCtx
    invoice: InvoiceCtx
    config: ConfigCtx
    notification: NotificationCtx = field(default_factory=NotificationCtx)
    now: datetime = field(default_factory=lambda: datetime(2026, 1, 1))
    debit_paise: int = 0
    post_debit_notification_queued: bool = False
    interest_computed_from: float | None = None
    promise_date: datetime | None = None

    def to_namespace(self) -> dict[str, object]:
        return {
            "debtor": self.debtor,
            "mandate": self.mandate,
            "action": self.action,
            "decision": self.decision,
            "invoice": self.invoice,
            "config": self.config,
            "notification": self.notification,
            "now": self.now,
            "debit_paise": self.debit_paise,
            "post_debit_notification_queued": self.post_debit_notification_queued,
            "interest_computed_from": self.interest_computed_from,
            "promise_date": self.promise_date,
            "ALL_CHANNELS": ALL_CHANNELS,
            "REQUIRED_PREDEBIT_FIELDS": REQUIRED_PREDEBIT_FIELDS,
        }
