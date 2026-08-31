"""The orchestrator: DIAGNOSE -> DECIDE -> BOUNDS -> ACT, run back to back,
for real, triggered by a live event -- not a human clicking through each
stage. DEVDOC_v6's own §10 stage table and Law 4.

This does NOT violate Law 4 ("Agents coordinate only through the ledger. No
stage calls another."). This module is the driver *outside* every agent --
the same role a human clicking through the demo dashboard was playing until
now -- calling each agent's own already-tested public function in turn and
writing each step's result to the ledger via the same `execute_action()`
chokepoint everything else uses. No agent here imports or calls another
agent directly; Diagnose doesn't know Decide exists, Decide doesn't know
Bounds exists, and so on.

Path A only for v1 (a structured Razorpay failure code -> a diagnosis, no
model call) -- Path B (agent.diagnose.llm_extract, a real Claude call) slots
into the exact same run_pipeline() by producing the same ExtractionResult
shape; wiring a live Telegram reply into this orchestrator is the natural
next step, not a redesign.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from agent.act.actions import ActionType
from agent.act.executor import ActionOutcome, ActionRefused, OutboundActionStore, execute_action
from agent.bounds.context import ActionCtx, BoundsContext, ConfigCtx, DebtorCtx, DecisionCtx, InvoiceCtx, MandateCtx
from agent.decide.ev import Prior, compute_ev
from agent.decide.fitted_p_base import load_fitted_p_base
from agent.diagnose.extract import ACTIONS_UNLOCKED, DiagnosisClass, ExtractionResult, Family
from agent.diagnose.taxonomy import FailureTaxonomy, UnknownFailureCode, default_taxonomy
from agent.ledger.store import Ledger
from agent.notify.protocol import MessageChannel
from agent.rails.protocol import Rail

DEFAULT_TOUCH_COST_PAISE = 500
"""Rs 5 nominal cost per touch -- a structural placeholder for compute_ev(),
same reasoning and same value as eval/simulate.py's. Not yet per-channel
(Telegram ~free, a call has real cost) -- a known simplification, not an
oversight; see docs/ORCHESTRATION.md."""

DEFAULT_LIFT = Prior(0.5)
"""The declared-prior sweep range's floor (§17.2: 0.5-4.0) -- an intuitive,
sub-face-value default for a live demo, not a claim this is the "true" lift."""


# --- Path A: a structured failure code -> DiagnosisClass, no model. ---
#
# Every code in data/failure_taxonomy.yaml maps somewhere below. Where no
# DiagnosisClass is an exact semantic match (most of them, honestly --
# Family A's taxonomy in agent.diagnose.extract wasn't derived FROM this
# file), the nearest defensible class is used and the reasoning is written
# down here, not left for someone to reverse-engineer later.
_CODE_TO_CLASS: dict[str, DiagnosisClass] = {
    "insufficient_funds": DiagnosisClass.INSUFFICIENT_FUNDS,
    "card_expired": DiagnosisClass.INSTRUMENT_EXPIRED,
    "transaction_limit_exceeded": DiagnosisClass.LIMIT_EXCEEDED,
    "authentication_failed": DiagnosisClass.AUTH_FAILURE,
    "incorrect_cvv": DiagnosisClass.AUTH_FAILURE,
    "bank_technical_error": DiagnosisClass.BANK_DOWNTIME,
    "gateway_technical_error": DiagnosisClass.BANK_DOWNTIME,
    "credit_failed_partner_downtime": DiagnosisClass.BANK_DOWNTIME,
    "payment_cancelled": DiagnosisClass.CUSTOMER_ABANDONED,
    "payment_timed_out": DiagnosisClass.CUSTOMER_ABANDONED,
    "payment_collect_request_expired": DiagnosisClass.CUSTOMER_ABANDONED,
    "card_declined": DiagnosisClass.AUTH_FAILURE,
    "payment_declined": DiagnosisClass.AUTH_FAILURE,
    # The instrument itself needs replacing/unblocking outside this system --
    # grouped under INSTRUMENT_EXPIRED ("this specific instrument can't be
    # used"), the nearest real class, not a precise match for any of these.
    "card_not_enrolled": DiagnosisClass.INSTRUMENT_EXPIRED,
    "card_disabled_for_online_payments": DiagnosisClass.INSTRUMENT_EXPIRED,
    "debit_instrument_blocked": DiagnosisClass.INSTRUMENT_EXPIRED,
    # Wrong/mismatched registration details -- closer to "the registered
    # instrument itself is invalid" than any Family A class about a working
    # instrument failing at payment time.
    "credit_failed_account_mismatch": DiagnosisClass.MANDATE_INVALID,
    "invalid_vpa": DiagnosisClass.MANDATE_INVALID,
    # A gateway-level resolution failure -- the rail itself, not the customer
    # or a specific bank.
    "vpa_resolution_failed": DiagnosisClass.RAIL_DEGRADED,
    # The bank explicitly flagged this as fraud, not a technical failure --
    # AUTH_FAILURE is the nearest class; the real gate against a blind retry
    # is `disposition == TERMINAL` (see failure_taxonomy.yaml's own note on
    # this exact code), not this class choice.
    "payment_risk_check_failed": DiagnosisClass.AUTH_FAILURE,
}


class UnmappedFailureCode(Exception):
    """A taxonomy code has no DiagnosisClass mapping -- raised loudly, never
    silently defaulted. Defaulting a diagnosis is exactly what this whole
    project's design exists to prevent."""


def diagnose_from_failure_code(
    code: str, rail: str | None = None, *, taxonomy: FailureTaxonomy | None = None
) -> ExtractionResult:
    """Path A -- no model call. confidence=1.0: a structured failure code is
    a fact from the rail, not a guess the way Path B's LLM read of free text
    is -- and downstream code (compute_ev, check_bounds) treats both
    identically regardless of that confidence value, per Law II.

    `rail` ("cards"/"upi") is optional: a real `payment.failed` webhook (and
    SimulatedRail's own) carries `error_code` but not which rail it came
    from without a separate lookup at the `method` field. Every code in
    _CODE_TO_CLASS maps to the same DiagnosisClass regardless of rail, so
    when `rail` is omitted this checks the code exists under *some* rail
    (still fails loudly on a code the taxonomy has never heard of) rather
    than requiring a rail this caller may not have."""
    taxonomy = taxonomy or default_taxonomy()
    if rail is not None:
        taxonomy.classify(code, rail)  # raises UnknownFailureCode if not a real (code, rail) pair
    elif code not in taxonomy.permitted_codes():
        raise UnknownFailureCode(f"no taxonomy entry for code={code!r} under any rail")
    diagnosis_class = _CODE_TO_CLASS.get(code)
    if diagnosis_class is None:
        raise UnmappedFailureCode(f"no DiagnosisClass mapping for taxonomy code {code!r}")
    return ExtractionResult(family=Family.A, class_=diagnosis_class, confidence=1.0)


def disposition_for_code(code: str, rail: str | None = None, *, taxonomy: FailureTaxonomy | None = None) -> str:
    """RETRYABLE/TERMINAL for select_action_for_diagnosis's disposition
    param -- same rail-optional search as diagnose_from_failure_code, for
    the same reason (a real webhook doesn't reliably carry which rail).
    Every code observed to appear under more than one rail in
    data/failure_taxonomy.yaml has the same disposition on both, so
    searching rather than requiring an exact rail doesn't paper over a
    real disagreement -- there isn't one today."""
    taxonomy = taxonomy or default_taxonomy()
    if rail is not None:
        return taxonomy.classify(code, rail).disposition
    for candidate_rail in ("cards", "upi"):
        if code in taxonomy.permitted_codes(candidate_rail):
            return taxonomy.classify(code, candidate_rail).disposition
    raise UnknownFailureCode(f"no taxonomy entry for code={code!r} under any rail")


def select_action_for_diagnosis(diagnosis: ExtractionResult, *, disposition: str | None = None) -> ActionType:
    """Which action a diagnosis unlocks -- always a member of
    ACTIONS_UNLOCKED[diagnosis.family] (asserted below at import time), so
    this can never propose something check_bounds()'s own family gate would
    refuse on principle."""
    if diagnosis.family == Family.A:
        # RETRYABLE (per failure_taxonomy.yaml) means retrying the same
        # instrument might just work next time; TERMINAL means it structurally
        # can't -- offer a fresh instrument instead of hammering a dead one.
        return ActionType.RETRY_CHARGE if disposition == "RETRYABLE" else ActionType.CREATE_PAYMENT_LINK
    if diagnosis.family == Family.B:
        return ActionType.REISSUE_ARTIFACT
    if diagnosis.family == Family.C:
        return ActionType.SEND_REMINDER
    if diagnosis.family == Family.D:
        return ActionType.ESCALATE_HUMAN
    raise ValueError(f"no action mapping for family {diagnosis.family!r}")  # pragma: no cover -- Family is exhaustive


for _action, _family in (
    (ActionType.RETRY_CHARGE, Family.A), (ActionType.CREATE_PAYMENT_LINK, Family.A),
    (ActionType.REISSUE_ARTIFACT, Family.B), (ActionType.SEND_REMINDER, Family.C),
    (ActionType.ESCALATE_HUMAN, Family.D),
):
    assert _action.value in ACTIONS_UNLOCKED[_family], (
        f"{_action!r} is not in ACTIONS_UNLOCKED[{_family!r}] -- select_action_for_diagnosis "
        "has drifted from agent.diagnose.extract's real table"
    )


def _payload_for_action(
    action_type: ActionType, *, amount_paise: int, to: str | None, text: str | None,
) -> dict:
    if action_type in (ActionType.CREATE_PAYMENT_LINK, ActionType.REISSUE_ARTIFACT):
        return {"amount_paise": amount_paise, "description": "TrueCommit recovery action"}
    if action_type == ActionType.RETRY_CHARGE:
        # No live mandate_id plumbed through yet at the orchestration layer --
        # RazorpayRail.present_debit() itself is still simulated-only (see
        # docs/LIMITATIONS.md), so this path is exercised against SimulatedRail.
        return {"mandate_id": "unknown", "amount_paise": amount_paise}
    if to is not None and text is not None:
        return {"to": to, "text": text}
    return {"template": action_type.value}  # falls back to the pre-existing stub behaviour, no real send


def touches_last_7_days(ledger: Ledger, debtor_id: str) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    return sum(
        1 for e in ledger.replay(debtor_id).entries
        if e.action is not None and e.ts is not None and e.ts >= cutoff
    )


@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    debtor_id: str
    invoice_id: str
    diagnosis: ExtractionResult
    action_type: ActionType
    ev_paise: int
    bounds_passed: bool
    refusal_reasons: list[str]
    action_outcome: ActionOutcome | None


def run_pipeline(
    *,
    debtor_id: str,
    invoice_id: str,
    amount_paise: int,
    diagnosis: ExtractionResult,
    channel_tag: str,
    ledger: Ledger,
    outbound_store: OutboundActionStore,
    rail: Rail,
    channel: MessageChannel | None = None,
    to: str | None = None,
    message_text: str | None = None,
    disposition: str | None = None,
    lift_prior: "Prior[float]" = DEFAULT_LIFT,
    touch_cost_paise: int = DEFAULT_TOUCH_COST_PAISE,
    decision_seq: int | None = None,
) -> OrchestrationResult:
    """DECIDE -> BOUNDS -> ACT, given a diagnosis already produced by either
    Path A (diagnose_from_failure_code, above) or Path B
    (agent.diagnose.llm_extract.extract_from_reply) -- this function doesn't
    care which. That's Law II in code: a diagnosis is a diagnosis regardless
    of whether it came from a rail's own error code or a live model call.
    """
    p_base = load_fitted_p_base().predict(amount_paise)
    action_type = select_action_for_diagnosis(diagnosis, disposition=disposition)

    decision = compute_ev(
        p_base=p_base, lift_prior=lift_prior, recoverable_paise=amount_paise,
        cost_paise=touch_cost_paise, action_type=action_type.value,
    )

    touches = touches_last_7_days(ledger, debtor_id)
    debtor_state = ledger.replay(debtor_id).current_state or "ENGAGED"

    ctx = BoundsContext(
        debtor=DebtorCtx(id=debtor_id, state=debtor_state, touches_7d=touches),
        mandate=MandateCtx(),
        action=ActionCtx(type=action_type.value, channel=channel_tag, rail_tag=getattr(rail, "rail_tag", None)),
        decision=DecisionCtx(ev_paise=decision.ev_paise),
        invoice=InvoiceCtx(id=invoice_id, recovery_attempts=touches),
        config=ConfigCtx(),
    )

    seq = decision_seq if decision_seq is not None else touches + 1
    payload = _payload_for_action(action_type, amount_paise=amount_paise, to=to, text=message_text)

    try:
        outcome = execute_action(
            action_type=action_type, debtor_id=debtor_id, invoice_id=invoice_id, decision_seq=seq,
            bounds_context=ctx, rail=rail, outbound_store=outbound_store, ledger=ledger,
            payload=payload, actor="ORCHESTRATOR", channel=channel,
        )
        return OrchestrationResult(
            debtor_id=debtor_id, invoice_id=invoice_id, diagnosis=diagnosis, action_type=action_type,
            ev_paise=decision.ev_paise, bounds_passed=True, refusal_reasons=[], action_outcome=outcome,
        )
    except ActionRefused as exc:
        return OrchestrationResult(
            debtor_id=debtor_id, invoice_id=invoice_id, diagnosis=diagnosis, action_type=action_type,
            ev_paise=decision.ev_paise, bounds_passed=False,
            refusal_reasons=[f"{v.rule_id}: {v.reason}" for v in exc.result.refusals], action_outcome=None,
        )
