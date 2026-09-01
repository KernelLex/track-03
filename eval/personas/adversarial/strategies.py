"""Adversarial personas — DEVDOC_v6 §24.3. Each strategy tries to exploit
a real stopping rule for indefinite delay at minimum cost. Run through the
actual `check_bounds()` gate (not a stand-in) — the same discipline
`eval/simulate.py`'s Arm C already applies — to prove the §24.2 fixes
(promise-credibility decay, `DISPUTE_FREEZE`'s scope, `CHANNEL_EXHAUSTION`)
actually stop each exploit in the real bounds engine, not just that the
fix exists on paper.

**`INJECTOR` is deliberately not simulated here.** Its exploit is prompt
injection through free debtor text, which this synthetic harness has no
mechanism for at all — there's no live model call and no free text
anywhere in `eval/simulate.py`'s pipeline. Building a fake text-injection
stand-in here would be strictly weaker evidence than what already exists:
`tests/agent/test_injection_resistance.py` (80 tests, a 40-case corpus)
already proves this against the real schema and action-set mapping. Redoing
a worse version of that here would look like coverage without adding any.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from agent.bounds.context import ALL_CHANNELS, ActionCtx, BoundsContext, ConfigCtx, DebtorCtx, DecisionCtx, InvoiceCtx, MandateCtx
from agent.bounds.engine import check_bounds

PROMISE_TRAILING_WINDOW = 5
"""Matches rules.yaml's own comment: "kept / (kept + broken) over the
trailing 5 promises" — not a number this module invented."""


class AdversarialStrategy(str, Enum):
    SERIAL_PROMISER = "SERIAL_PROMISER"
    DISPUTE_ABUSER = "DISPUTE_ABUSER"
    CHANNEL_HOPPER = "CHANNEL_HOPPER"


@dataclass(frozen=True, slots=True)
class ContactAttempt:
    day: int
    allowed: bool
    action_type: str
    refusal_reasons: list[str]


@dataclass(frozen=True, slots=True)
class AdversarialRunResult:
    persona_id: str
    strategy: AdversarialStrategy
    attempts: list[ContactAttempt]
    ever_recontacted_or_escalated: bool
    """The real question §24.3 asks: did the exploit succeed in making
    this case permanently unreachable, or did the system eventually
    recontact the debtor (a legitimate touch actually landing) or route
    the case to a human? False here is the failure mode DEVDOC_v6's own
    §24.2 finding describes -- this must be True for every persona, or the
    fix doesn't actually work."""
    permanently_stalled: bool


def _base_ctx(
    *, debtor: DebtorCtx, invoice: InvoiceCtx, action: ActionCtx, config: ConfigCtx | None = None,
    now: datetime | None = None, promise_date: datetime | None = None,
) -> BoundsContext:
    kwargs: dict = dict(
        debtor=debtor, mandate=MandateCtx(), action=action,
        decision=DecisionCtx(ev_paise=100_000), invoice=invoice, config=config or ConfigCtx(),
    )
    if now is not None:
        kwargs["now"] = now
    if promise_date is not None:
        kwargs["promise_date"] = promise_date
    return BoundsContext(**kwargs)


def run_serial_promiser(persona_id: str, *, window_days: int, amount_paise: int, check_in_every_days: int = 5) -> AdversarialRunResult:
    """Promises on every contact, never pays. The exploit this rule closes:
    a hard cooldown that resets in full on each new promise lets a debtor
    stall forever for the cost of one sentence per cycle. The fix scales
    the cooldown by promise_credibility (kept/(kept+broken) over the
    trailing 5 promises), which decays toward ConfigCtx's
    promise_credibility_floor as promises keep breaking -- so a serial
    promiser's own cooldown shrinks the more they exploit it."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    config = ConfigCtx()
    attempts: list[ContactAttempt] = []
    broken_history: list[bool] = []  # True = broken, most recent last
    promise_date: datetime | None = None
    debtor_state = "ENGAGED"
    ever_recontacted = False

    day = 1
    while day <= window_days:
        if promise_date is not None and day < (promise_date - now).days:
            day += 1
            continue

        credibility = 1.0 if not broken_history else (
            sum(1 for b in broken_history[-PROMISE_TRAILING_WINDOW:] if not b) / len(broken_history[-PROMISE_TRAILING_WINDOW:])
        )
        ctx = _base_ctx(
            debtor=DebtorCtx(id=persona_id, state=debtor_state, promise_credibility=credibility),
            invoice=InvoiceCtx(id=f"inv_{persona_id}", recovery_attempts=len(attempts)),
            action=ActionCtx(type="send_reminder", channel="telegram", rail_tag="simulated"),
            config=config, now=now + timedelta(days=day), promise_date=promise_date,
        )
        result = check_bounds(ctx)
        attempts.append(ContactAttempt(
            day=day, allowed=result.passed, action_type="send_reminder",
            refusal_reasons=[v.rule_id for v in result.refusals],
        ))
        if result.passed:
            ever_recontacted = True
            # The persona promises again -- and this promise will be broken,
            # since a serial promiser by definition never pays.
            promise_date = now + timedelta(days=day + check_in_every_days)
            debtor_state = "PROMISED"
            broken_history.append(True)
            day += 1
        else:
            day += 1

    return AdversarialRunResult(
        persona_id=persona_id, strategy=AdversarialStrategy.SERIAL_PROMISER, attempts=attempts,
        ever_recontacted_or_escalated=ever_recontacted, permanently_stalled=not ever_recontacted,
    )


def run_dispute_abuser(persona_id: str, *, window_days: int, amount_paise: int) -> AdversarialRunResult:
    """Asserts an unsubstantiated dispute on first contact. The correct
    outcome is NOT "the system finds a way to collect anyway" -- it's that
    `escalate_human` (and only that) passes, every time, for the rest of
    the window: a human reviews it, the case is never silently abandoned."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    attempts: list[ContactAttempt] = []
    ever_escalated = False

    for day in (1, 8, 15, 22, 29):
        if day > window_days:
            break
        # First contact: debtor disputes. From here on, state is DISPUTED_FROZEN.
        debtor = DebtorCtx(id=persona_id, state="DISPUTED_FROZEN" if attempts else "ENGAGED")
        invoice = InvoiceCtx(id=f"inv_{persona_id}", recovery_attempts=len(attempts), disputed_paise=amount_paise)

        # Try the exploit's own "just collect anyway" action first.
        reminder_ctx = _base_ctx(debtor=debtor, invoice=invoice, action=ActionCtx(type="send_reminder", channel="telegram", rail_tag="simulated"))
        reminder_result = check_bounds(reminder_ctx)

        # Then the correct response: escalate. Channel-less on purpose --
        # routing to a human queue isn't itself a commercial communication
        # on any channel, so TRAI_DND (which only cares whether *this*
        # action's channel is one the debtor opted out of) shouldn't apply
        # to it at all; tagging it with the debtor's last-used channel
        # would incorrectly let that unrelated rule block escalation too.
        escalate_ctx = _base_ctx(debtor=debtor, invoice=invoice, action=ActionCtx(type="escalate_human", channel=None, rail_tag="simulated"))
        escalate_result = check_bounds(escalate_ctx)

        attempts.append(ContactAttempt(
            day=day, allowed=reminder_result.passed, action_type="send_reminder",
            refusal_reasons=[v.rule_id for v in reminder_result.refusals],
        ))
        attempts.append(ContactAttempt(
            day=day, allowed=escalate_result.passed, action_type="escalate_human",
            refusal_reasons=[v.rule_id for v in escalate_result.refusals],
        ))
        if escalate_result.passed:
            ever_escalated = True

    return AdversarialRunResult(
        persona_id=persona_id, strategy=AdversarialStrategy.DISPUTE_ABUSER, attempts=attempts,
        ever_recontacted_or_escalated=ever_escalated, permanently_stalled=not ever_escalated,
    )


def run_channel_hopper(persona_id: str, *, window_days: int, amount_paise: int) -> AdversarialRunResult:
    """Opts out of one channel per contact. Once every channel is opted
    out, only escalate_human/no_action/a regulatory notice may still pass
    -- the case must route to a human, not go silent."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    channels = sorted(ALL_CHANNELS)
    opted_out: set[str] = set()
    attempts: list[ContactAttempt] = []
    ever_escalated = False

    for i, day in enumerate((1, 8, 15, 22, 29, 36)):
        if day > window_days or i >= len(channels):
            break
        channel = channels[i]
        debtor = DebtorCtx(id=persona_id, state="ENGAGED", opted_out_channels=frozenset(opted_out))
        invoice = InvoiceCtx(id=f"inv_{persona_id}", recovery_attempts=len(attempts))

        reminder_ctx = _base_ctx(debtor=debtor, invoice=invoice, action=ActionCtx(type="send_reminder", channel=channel, rail_tag="simulated"))
        reminder_result = check_bounds(reminder_ctx)
        attempts.append(ContactAttempt(
            day=day, allowed=reminder_result.passed, action_type=f"send_reminder:{channel}",
            refusal_reasons=[v.rule_id for v in reminder_result.refusals],
        ))
        opted_out.add(channel)

        if len(opted_out) >= len(ALL_CHANNELS):
            # Channel-less, same reasoning as run_dispute_abuser's escalation.
            escalate_ctx = _base_ctx(
                debtor=DebtorCtx(id=persona_id, state="ENGAGED", opted_out_channels=frozenset(opted_out)),
                invoice=invoice, action=ActionCtx(type="escalate_human", channel=None, rail_tag="simulated"),
            )
            escalate_result = check_bounds(escalate_ctx)
            attempts.append(ContactAttempt(
                day=day, allowed=escalate_result.passed, action_type="escalate_human",
                refusal_reasons=[v.rule_id for v in escalate_result.refusals],
            ))
            ever_escalated = escalate_result.passed

    return AdversarialRunResult(
        persona_id=persona_id, strategy=AdversarialStrategy.CHANNEL_HOPPER, attempts=attempts,
        ever_recontacted_or_escalated=ever_escalated, permanently_stalled=not ever_escalated,
    )


STRATEGY_RUNNERS = {
    AdversarialStrategy.SERIAL_PROMISER: run_serial_promiser,
    AdversarialStrategy.DISPUTE_ABUSER: run_dispute_abuser,
    AdversarialStrategy.CHANNEL_HOPPER: run_channel_hopper,
}
