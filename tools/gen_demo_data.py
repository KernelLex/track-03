#!/usr/bin/env python3
"""One-off: compute real check_bounds()/compute_ev()/select_instrument()
output for the two demo dashboard scenarios (docs/DEMO_UI.md), so the
published Artifact's "scripted" pipeline walk displays real code output,
not hand-typed numbers. Prints JSON to stdout -- paste into the artifact,
don't hand-edit the numbers there.
"""

from __future__ import annotations

import json

from agent.bounds.context import ActionCtx, BoundsContext, ConfigCtx, DebtorCtx, DecisionCtx, InvoiceCtx, MandateCtx
from agent.bounds.engine import check_bounds
from agent.decide.ev import Prior, compute_ev
from agent.decide.fitted_p_base import load_fitted_p_base
from agent.mandate.instrument import Promise, select_instrument


def b2b_scenario() -> dict:
    amount_paise = 42_500_00
    p_base_model = load_fitted_p_base()
    p_base = p_base_model.predict(amount_paise)
    decision = compute_ev(
        p_base=p_base, lift_prior=Prior(0.5), recoverable_paise=amount_paise,
        cost_paise=500, action_type="create_payment_link",
    )
    instrument = select_instrument(Promise(total_amount_paise=amount_paise))
    ctx = BoundsContext(
        debtor=DebtorCtx(id="demo_b2b", state="ENGAGED", touches_7d=1),
        mandate=MandateCtx(),
        action=ActionCtx(type="create_payment_link", channel="telegram", rail_tag="simulated"),
        decision=DecisionCtx(ev_paise=decision.ev_paise),
        invoice=InvoiceCtx(id="INV-2201", recovery_attempts=1),
        config=ConfigCtx(),
    )
    bounds = check_bounds(ctx)
    return {
        "amount_paise": amount_paise,
        "p_base": round(p_base, 4),
        "ev_paise": decision.ev_paise,
        "instrument": instrument.instrument.value,
        "instrument_rationale": instrument.rationale,
        "bounds_passed": bounds.passed,
        "bounds_rules_checked": len(bounds.verdicts),
        "bounds_rules_passed": sum(1 for v in bounds.verdicts if v.verdict == "PASS"),
    }


def subscription_scenario() -> dict:
    amount_paise = 999_00
    p_base_model = load_fitted_p_base()
    p_base = p_base_model.predict(amount_paise)
    decision = compute_ev(
        p_base=p_base, lift_prior=Prior(0.5), recoverable_paise=amount_paise,
        cost_paise=500, action_type="repair_mandate",
    )
    instrument = select_instrument(Promise(total_amount_paise=amount_paise, installments=12, installment_amount_paise=amount_paise))
    ctx = BoundsContext(
        debtor=DebtorCtx(id="demo_sub", state="AT_RISK", touches_7d=0),
        mandate=MandateCtx(status="health_defect", afa_required=False),
        action=ActionCtx(type="repair_mandate", channel="telegram", rail_tag="simulated"),
        decision=DecisionCtx(ev_paise=decision.ev_paise),
        invoice=InvoiceCtx(id="SUB-8834", recovery_attempts=0),
        config=ConfigCtx(),
    )
    bounds = check_bounds(ctx)
    return {
        "amount_paise": amount_paise,
        "p_base": round(p_base, 4),
        "ev_paise": decision.ev_paise,
        "instrument": instrument.instrument.value,
        "instrument_rationale": instrument.rationale,
        "bounds_passed": bounds.passed,
        "bounds_rules_checked": len(bounds.verdicts),
        "bounds_rules_passed": sum(1 for v in bounds.verdicts if v.verdict == "PASS"),
    }


def escalation_scenario() -> dict:
    """A disputed invoice -- Bounds must refuse a mandate/reminder and only
    escalate_human passes, per ACTIONS_UNLOCKED[Family.D]."""
    amount_paise = 88_000_00
    disputed_paise = 30_000_00

    blocked_decision = compute_ev(
        p_base=0.6, lift_prior=Prior(0.5), recoverable_paise=disputed_paise,
        cost_paise=500, action_type="create_mandate",
    )
    ctx_blocked = BoundsContext(
        debtor=DebtorCtx(id="demo_dispute", state="ENGAGED", touches_7d=2),
        mandate=MandateCtx(),
        action=ActionCtx(type="create_mandate", channel="telegram", rail_tag="simulated"),
        decision=DecisionCtx(ev_paise=blocked_decision.ev_paise),
        invoice=InvoiceCtx(id="INV-5581", recovery_attempts=2, disputed_paise=disputed_paise),
        config=ConfigCtx(),
    )
    blocked = check_bounds(ctx_blocked)

    # A human reviewing a disputed portion has a real, computable EV too --
    # not exempt from EV_FLOOR, just weighed against a human-review cost
    # instead of a message cost.
    escalate_decision = compute_ev(
        p_base=0.5, lift_prior=Prior(0.5), recoverable_paise=disputed_paise,
        cost_paise=50_000, action_type="escalate_human",
    )
    ctx_escalate = BoundsContext(
        debtor=DebtorCtx(id="demo_dispute", state="ENGAGED", touches_7d=2),
        mandate=MandateCtx(),
        action=ActionCtx(type="escalate_human", rail_tag="simulated"),
        decision=DecisionCtx(ev_paise=escalate_decision.ev_paise),
        invoice=InvoiceCtx(id="INV-5581", recovery_attempts=2, disputed_paise=disputed_paise),
        config=ConfigCtx(),
    )
    escalate = check_bounds(ctx_escalate)
    return {
        "amount_paise": amount_paise,
        "disputed_paise": disputed_paise,
        "undisputed_paise": amount_paise - disputed_paise,
        "mandate_action_refused": not blocked.passed,
        "mandate_refusal_rules": [v.rule_id for v in blocked.refusals],
        "escalate_ev_paise": escalate_decision.ev_paise,
        "escalate_human_passed": escalate.passed,
    }


if __name__ == "__main__":
    print(json.dumps({
        "b2b": b2b_scenario(),
        "subscription": subscription_scenario(),
        "escalation": escalation_scenario(),
    }, indent=2))
