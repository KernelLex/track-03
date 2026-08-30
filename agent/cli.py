"""The `trucommit` CLI. DEVDOC_v6 §19: `uv run trucommit demo` is the ten-minute
promise — if this doesn't work from a clean clone, SETUP.md is lying.

`demo` here is honestly scoped to what's actually built so far (see
docs/LIMITATIONS.md for what isn't yet): the ledger, the bounds gate, the
debtor state machine, instrument selection, and SimulatedRail, wired
together end to end on one synthetic invoice. It is not the four-arm eval
(§17) — that needs personas and pre-registration that don't exist yet — and
this command says so rather than implying otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


def _demo() -> int:
    from agent.bounds.context import ActionCtx, BoundsContext, ConfigCtx, DebtorCtx, DecisionCtx, InvoiceCtx, MandateCtx
    from agent.bounds.engine import check_bounds
    from agent.diagnose.state_machine import DebtorState, transition
    from agent.ledger.models import LedgerEntry
    from agent.ledger.recovery import RecoveryLedger
    from agent.ledger.store import Ledger
    from agent.mandate.instrument import Promise, select_instrument
    from agent.rails.simulated import SimulatedRail
    from agent.rails.types import LinkSpec

    print("=" * 72)
    print("TrueCommit demo -- one debtor, end to end, on the pieces built so far")
    print("=" * 72)

    with tempfile.TemporaryDirectory() as tmp:
        with Ledger(Path(tmp) / "ledger.db") as ledger, RecoveryLedger(Path(tmp) / "recovery.db") as recovery:
            debtor_id = "debtor_demo_1"

            state = DebtorState.HEALTHY
            print(f"\n[state machine] {state.value}")
            for target in (DebtorState.AT_RISK, DebtorState.DIAGNOSED, DebtorState.ENGAGED):
                state = transition(state, target)
                print(f"[state machine] -> {state.value}")

            promise = Promise(total_amount_paise=12_000_00)  # Rs 12,000 -- under the AFA-free ceiling
            spec = select_instrument(promise)
            print(f"\n[instrument] {spec.instrument.value} for Rs {spec.amount_paise / 100:,.2f} -- {spec.rationale}")

            ctx = BoundsContext(
                debtor=DebtorCtx(id=debtor_id, state=state.value, touches_7d=1),
                mandate=MandateCtx(),
                action=ActionCtx(type="create_payment_link", channel="email", rail_tag="simulated"),
                decision=DecisionCtx(ev_paise=spec.amount_paise - 500),
                invoice=InvoiceCtx(id="inv_demo_1", recovery_attempts=1),
                config=ConfigCtx(),
            )
            result = check_bounds(ctx)
            passed_count = sum(1 for v in result.verdicts if v.verdict == "PASS")
            print(f"\n[bounds] {'PASS' if result.passed else 'REFUSE'} "
                  f"({passed_count}/{len(result.verdicts)} rules passed)")
            if not result.passed:
                for refusal in result.refusals:
                    print(f"[bounds]   refused by {refusal.rule_id}: {refusal.reason}")
                return 1

            rail = SimulatedRail(webhook_secret="demo-secret")
            link = rail.create_payment_link(LinkSpec(amount_paise=spec.amount_paise, description="Demo invoice"))
            print(f"\n[rail:simulated] created {link.id} for Rs {link.amount_paise / 100:,.2f}")
            rail.simulate_link_paid(link.id)
            print(f"[rail:simulated] {link.id} paid")

            webhook = rail.emitted_webhooks[-1]
            payment_entity = json.loads(webhook.body)["payload"]["payment"]["entity"]

            entry = recovery.attribute(
                payment_id=payment_entity["id"], payment_status=payment_entity["status"],
                invoice_id="inv_demo_1", debtor_id=debtor_id,
                amount_paise=payment_entity["amount"], rail_tag="simulated",
            )
            print(f"[recovery_ledger] attributed {entry.payment_id} -- Rs {entry.amount_paise / 100:,.2f} "
                  f"(rail_tag={entry.rail_tag})")

            for target in (DebtorState.PROMISED, DebtorState.INSTRUMENTED, DebtorState.RECOVERED):
                state = transition(state, target)
                print(f"[state machine] -> {state.value}")

            ledger.append(LedgerEntry(
                actor="DEMO", debtor_id=debtor_id,
                outcome={"debtor_state_after": state.value, "recovered_paise": entry.amount_paise},
            ))
            ledger.verify_chain()
            entry_count = len(list(ledger.all_entries()))
            print(f"[ledger] {entry_count} entr{'y' if entry_count == 1 else 'ies'} recorded, hash chain verified")

    print("\n" + "=" * 72)
    print("Demo complete. This is NOT the four-arm eval (DEVDOC_v6 Section 17) -- that needs")
    print("personas and a pre-registration commit that don't exist yet.")
    print("See DEVDOC_v6.md and docs/LIMITATIONS.md for what's built vs pending.")
    print("=" * 72)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trucommit", description="TrueCommit -- bounded AR-recovery agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("demo", help="Run a small real end-to-end demo on one synthetic debtor")

    args = parser.parse_args(argv)
    if args.command == "demo":
        return _demo()
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
