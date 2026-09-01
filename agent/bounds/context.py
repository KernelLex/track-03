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

WHATSAPP_SESSION_WINDOW_DAYS = 1
"""Meta's WhatsApp customer-service window: 24 hours from the debtor's own
last message. Expressed in days because the rule language's `timedelta()`
takes days, and 24 hours is exactly one.

A platform rule, not law -- see WHATSAPP_SESSION_WINDOW in rules.yaml for
why that distinction decides which register it lives in."""

ALL_CHANNELS: frozenset[str] = frozenset({"sms", "email", "whatsapp", "ivr", "telegram"})


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
    uses_approved_template: bool = False
    """True when the send goes out as a pre-approved WhatsApp template
    rather than free-form text. That is the only legal way to open a
    conversation outside Meta's 24-hour customer-service window, so
    WHATSAPP_SESSION_WINDOW exempts it -- see that rule."""
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
    last_inbound_at: datetime | None = None
    """When the debtor last messaged us, for WHATSAPP_SESSION_WINDOW.

    None means they never have -- which is the cold-outreach case, and the
    one the rule refuses a free-form send for. Not a timestamp this system
    chooses: it comes from the channel's own record of an inbound message."""

    def to_dict(self) -> dict[str, object]:
        """A JSON-safe snapshot, for storing inside a LedgerEntry's `action`
        field so the Auditor's bounds-integrity job (§11.7) can later
        reconstruct this exact context and recompute check_bounds() from
        ledger inputs alone — not from live, possibly-since-changed state."""
        return {
            "debtor": {
                "id": self.debtor.id, "state": self.debtor.state, "touches_7d": self.debtor.touches_7d,
                "opted_out_cycle": self.debtor.opted_out_cycle,
                "opted_out_channels": sorted(self.debtor.opted_out_channels),
                "local_time": self.debtor.local_time.isoformat(),
                "promise_credibility": self.debtor.promise_credibility,
            },
            "mandate": {
                "id": self.mandate.id, "status": self.mandate.status,
                "last_notification_at": self.mandate.last_notification_at.isoformat() if self.mandate.last_notification_at else None,
                "afa_required": self.mandate.afa_required,
            },
            "action": {
                "type": self.action.type, "channel": self.action.channel, "afa_reference": self.action.afa_reference,
                "human_approval_id": self.action.human_approval_id, "carries_legal_number": self.action.carries_legal_number,
                "rail_tag": self.action.rail_tag, "is_regulatory_notice": self.action.is_regulatory_notice,
                "presents_mandate_debit": self.action.presents_mandate_debit,
                "uses_approved_template": self.action.uses_approved_template,
                "params": self.action.params, "debtor_stated_params": self.action.debtor_stated_params,
                "clamp_direction": self.action.clamp_direction,
            },
            "decision": {"ev_paise": self.decision.ev_paise},
            "invoice": {
                "id": self.invoice.id, "recovery_attempts": self.invoice.recovery_attempts,
                "disputed_paise": self.invoice.disputed_paise,
            },
            "config": {
                "promise_credibility_floor": self.config.promise_credibility_floor,
                "grace_days": self.config.grace_days, "rbi_bank_rate": self.config.rbi_bank_rate,
                "as_of_age_days": self.config.as_of_age_days,
            },
            "notification": {"fields": sorted(self.notification.fields)},
            "now": self.now.isoformat(),
            "debit_paise": self.debit_paise,
            "post_debit_notification_queued": self.post_debit_notification_queued,
            "interest_computed_from": self.interest_computed_from,
            "promise_date": self.promise_date.isoformat() if self.promise_date else None,
            "last_inbound_at": self.last_inbound_at.isoformat() if self.last_inbound_at else None,
        }

    @staticmethod
    def from_dict(data: dict) -> "BoundsContext":
        d, m, a, dec, inv, cfg, notif = (
            data["debtor"], data["mandate"], data["action"], data["decision"],
            data["invoice"], data["config"], data["notification"],
        )
        return BoundsContext(
            debtor=DebtorCtx(
                id=d["id"], state=d["state"], touches_7d=d["touches_7d"], opted_out_cycle=d["opted_out_cycle"],
                opted_out_channels=frozenset(d["opted_out_channels"]),
                local_time=time.fromisoformat(d["local_time"]), promise_credibility=d["promise_credibility"],
            ),
            mandate=MandateCtx(
                id=m["id"], status=m["status"],
                last_notification_at=datetime.fromisoformat(m["last_notification_at"]) if m["last_notification_at"] else None,
                afa_required=m["afa_required"],
            ),
            action=ActionCtx(
                type=a["type"], channel=a["channel"], afa_reference=a["afa_reference"],
                human_approval_id=a["human_approval_id"], carries_legal_number=a["carries_legal_number"],
                rail_tag=a["rail_tag"], is_regulatory_notice=a["is_regulatory_notice"],
                presents_mandate_debit=a["presents_mandate_debit"],
                # .get for the same reason as last_inbound_at: the Auditor
                # recomputes check_bounds() from entries written before this
                # field existed.
                uses_approved_template=a.get("uses_approved_template", False),
                params=a["params"],
                debtor_stated_params=a["debtor_stated_params"], clamp_direction=a["clamp_direction"],
            ),
            decision=DecisionCtx(ev_paise=dec["ev_paise"]),
            invoice=InvoiceCtx(id=inv["id"], recovery_attempts=inv["recovery_attempts"], disputed_paise=inv["disputed_paise"]),
            config=ConfigCtx(
                promise_credibility_floor=cfg["promise_credibility_floor"], grace_days=cfg["grace_days"],
                rbi_bank_rate=cfg["rbi_bank_rate"], as_of_age_days=cfg["as_of_age_days"],
            ),
            notification=NotificationCtx(fields=frozenset(notif["fields"])),
            now=datetime.fromisoformat(data["now"]),
            debit_paise=data["debit_paise"],
            post_debit_notification_queued=data["post_debit_notification_queued"],
            interest_computed_from=data["interest_computed_from"],
            promise_date=datetime.fromisoformat(data["promise_date"]) if data["promise_date"] else None,
            # .get, not [], so a ledger entry written before this field
            # existed still reconstructs -- the Auditor recomputes
            # check_bounds() from old entries and must not choke on them.
            last_inbound_at=(datetime.fromisoformat(data["last_inbound_at"])
                             if data.get("last_inbound_at") else None),
        )

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
            "last_inbound_at": self.last_inbound_at,
            "ALL_CHANNELS": ALL_CHANNELS,
            "REQUIRED_PREDEBIT_FIELDS": REQUIRED_PREDEBIT_FIELDS,
            "WHATSAPP_SESSION_WINDOW_DAYS": WHATSAPP_SESSION_WINDOW_DAYS,
        }
