"""The pre-debit notification content builder. DEVDOC_v6 §12.4.

Mandatory fields (matching `RBI_EMANDATE_PREDEBIT_24H`'s
`REQUIRED_PREDEBIT_FIELDS` in `agent/bounds/context.py`, so the two can't
drift apart): merchant_name, amount, debit_datetime, mandate_ref, reason.

"Fully demonstrable without mandate rails. The message is real; only the
debit behind it is simulated." This builder produces the real message
content; `agent/rails/simulated.py::notify_predebit()` is what actually
"sends" it (as a signed webhook, in this build).

Beyond the five mandatory fields, this build's own enrichments per §12.4:
a balance nudge on REPEAT_NSF, a reschedule option, the AFA link inline
above the Rs 15,000 ceiling, and a pay-early link. Always carries an
opt-out — the debtor is invited to interact, not just informed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from agent.bounds.context import REQUIRED_PREDEBIT_FIELDS
from agent.mandate.health import REPEAT_NSF_THRESHOLD
from agent.mandate.instrument import AFA_FREE_CEILING_PAISE


class MissingAfaLink(Exception):
    """Raised when amount exceeds the AFA-free ceiling but no afa_url was
    supplied — DEVDOC_v6 §12.2: "the AFA link ships inside the mandatory
    pre-debit notification." A notification built without it would be
    non-compliant, so this refuses to build one rather than silently omitting it."""


@dataclass(frozen=True, slots=True)
class PredebitNotificationContext:
    merchant_name: str
    amount_paise: int
    debit_datetime: datetime
    mandate_ref: str
    reason: str
    opt_out_url: str
    reschedule_url: str
    pay_early_url: str
    consecutive_nsf: int = 0
    afa_url: str | None = None


@dataclass(frozen=True, slots=True)
class PredebitNotification:
    fields: dict[str, object]
    """Exactly REQUIRED_PREDEBIT_FIELDS' keys — what RBI_EMANDATE_PREDEBIT_24H
    checks notification.fields against."""
    body_lines: tuple[str, ...]


def build_predebit_notification(ctx: PredebitNotificationContext) -> PredebitNotification:
    if ctx.amount_paise > AFA_FREE_CEILING_PAISE and ctx.afa_url is None:
        raise MissingAfaLink(
            f"amount_paise={ctx.amount_paise} exceeds the Rs 15,000 AFA-free ceiling "
            "but no afa_url was supplied"
        )

    fields: dict[str, object] = {
        "merchant_name": ctx.merchant_name,
        "amount": ctx.amount_paise,
        "debit_datetime": ctx.debit_datetime.isoformat(),
        "mandate_ref": ctx.mandate_ref,
        "reason": ctx.reason,
    }
    assert set(fields) == set(REQUIRED_PREDEBIT_FIELDS), "notification fields drifted from REQUIRED_PREDEBIT_FIELDS"

    body_lines = [
        f"{ctx.merchant_name} will debit Rs {ctx.amount_paise / 100:,.2f} from your account "
        f"on {ctx.debit_datetime.isoformat()} under mandate {ctx.mandate_ref} ({ctx.reason}).",
        f"Opt out of this debit: {ctx.opt_out_url}",
        f"Reschedule: {ctx.reschedule_url}",
        f"Pay early instead: {ctx.pay_early_url}",
    ]

    if ctx.consecutive_nsf >= REPEAT_NSF_THRESHOLD:
        body_lines.append(
            "Note: the last two attempts on this mandate did not go through. "
            "Please ensure sufficient balance is available before the debit date."
        )

    if ctx.afa_url is not None:
        body_lines.append(f"This debit is above Rs 15,000 and requires your authentication: {ctx.afa_url}")

    return PredebitNotification(fields=fields, body_lines=tuple(body_lines))
