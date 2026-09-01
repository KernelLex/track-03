"""The `trucommit` CLI. DEVDOC_v6 §19: `uv run trucommit demo` is the ten-minute
promise — if this doesn't work from a clean clone, SETUP.md is lying.

`demo` here is honestly scoped (see docs/LIMITATIONS.md for what isn't
built): the ledger, the bounds gate, the debtor state machine, instrument
selection, and SimulatedRail, wired together end to end on one synthetic
invoice.

It is deliberately *not* the evaluation. That is a separate, larger
artifact and it does exist -- `eval/PREREGISTRATION.md` locks
n=500/seed=42/window=30d/lift=1.0 at its own commit, before
`eval/report.py` generated `docs/RESULTS.md` from exactly that
configuration. This docstring and this command's closing message both used
to say the personas and pre-registration "don't exist yet", which stopped
being true once they were built and went unnoticed for long enough that an
external reviewer running the first command in the README was told the
strongest artifact in the repo was missing. Pointing at it instead.
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
    print("Demo complete -- this is one debtor through the real pipeline, not the evaluation.")
    print("The evaluation is a separate artifact and it exists:")
    print("  eval/PREREGISTRATION.md  -- n=500/seed=42/window=30d/lift=1.0, locked before the run")
    print("  docs/RESULTS.md          -- generated from exactly that config: uv run python eval/report.py")
    print("See docs/LIMITATIONS.md for what is genuinely still cut, and why.")
    print("=" * 72)
    return 0


def _serve(host: str, port: int) -> int:
    import uvicorn

    print(f"Starting the webhook receiver on http://{host}:{port} (see agent/api/app.py).")
    print("Configure TRUECOMMIT_WEBHOOK_SECRET_<SOURCE> env vars before pointing a real webhook at this.")
    uvicorn.run("agent.api.app:app", host=host, port=port)
    return 0


def _simulate(n: int, seed: int, window_days: int, lift: float, touch_cost_paise: int) -> int:
    from eval.simulate import _print_summary, run_comparison

    print("Not a pre-registered run -- see eval/PREREGISTRATION.md and eval/simulate.py's module docstring.")
    print(f"n={n} seed={seed} window_days={window_days} lift={lift} touch_cost_paise={touch_cost_paise}\n")
    summaries = run_comparison(
        n_personas=n, seed=seed, window_days=window_days, lift=lift, touch_cost_paise=touch_cost_paise,
    )
    _print_summary(summaries)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trucommit", description="TrueCommit -- bounded AR-recovery agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("demo", help="Run a small real end-to-end demo on one synthetic debtor")
    serve_parser = subparsers.add_parser("serve", help="Run the webhook receiver (agent/api/app.py) with uvicorn")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    sim_parser = subparsers.add_parser(
        "simulate", help="Monte Carlo comparison of Arms A/B2/C over a synthetic population (eval/simulate.py)"
    )
    sim_parser.add_argument("--n", type=int, default=300)
    sim_parser.add_argument("--seed", type=int, default=1)
    sim_parser.add_argument("--window-days", type=int, default=30)
    sim_parser.add_argument("--lift", type=float, default=2.0)
    sim_parser.add_argument("--touch-cost-paise", type=int, default=500)

    args = parser.parse_args(argv)
    if args.command == "demo":
        return _demo()
    if args.command == "serve":
        return _serve(args.host, args.port)
    if args.command == "simulate":
        return _simulate(args.n, args.seed, args.window_days, args.lift, args.touch_cost_paise)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
