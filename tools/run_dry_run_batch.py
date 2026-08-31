#!/usr/bin/env python3
"""A real batch of decisions, zero rupees moved: N synthetic invoices,
each spanning one of the full 29 real DiagnosisClass values across all
four families, pushed through the actual pipeline (agent.orchestrate.
run_pipeline) with dry_run=True -- real DECIDE (the fitted p_base model,
real compute_ev()), real BOUNDS (the actual check_bounds() gate), and ACT
logging what it would have dispatched instead of dispatching it.

This is the concrete answer to "test mode success doesn't predict a real
debit clearing, so what CAN you safely test": dry_run mode doesn't predict
an outcome either -- it proves the judgment. Every decision below is real;
none of them can move a rupee, because dry_run never claims the outbound
idempotency key or calls the rail (agent/act/executor.py).

    uv run python tools/run_dry_run_batch.py --n 500

Writes docs/evidence/DRY_RUN_BATCH.md (human-readable) and
docs/evidence/dry_run_batch_<timestamp>.json (raw), plus a dedicated,
committed ledger at docs/evidence/dry_run_batch_ledger.db so
ledger.verify_chain() can be re-run against this exact evidence later --
same pattern as tools/run_real_scenarios.py's live batch, the dry-run
complement to it.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from agent.act.executor import OutboundActionStore
from agent.decide.fitted_p_base import load_fitted_p_base
from agent.diagnose.extract import FAMILY_CLASSES, ExtractionResult, Family
from agent.ledger.store import Ledger
from agent.orchestrate import run_pipeline
from agent.rails.simulated import SimulatedRail

EVIDENCE_DIR = Path(__file__).resolve().parents[1] / "docs" / "evidence"
LEDGER_PATH = EVIDENCE_DIR / "dry_run_batch_ledger.db"
OUTBOUND_PATH = EVIDENCE_DIR / "dry_run_batch_outbound.db"

ALL_CLASSES: list[tuple[Family, "object"]] = [
    (family, class_) for family, classes in FAMILY_CLASSES.items() for class_ in sorted(classes, key=lambda c: c.value)
]
"""Every real (Family, DiagnosisClass) pair -- 29 total, all four families
-- not a coarse 4-bucket stand-in. Sorted for a deterministic cycle order
given a fixed seed."""

MEDIAN_AMOUNT_PAISE = 50_000_00  # Rs 50,000 -- same population-scale assumption eval/personas/generator.py uses


def _sample_amount_paise(rng: random.Random) -> int:
    draw = max(0.05, rng.gauss(1.0, 0.6))
    return max(100_00, round(draw * MEDIAN_AMOUNT_PAISE / 100) * 100)


def generate_batch(n: int, *, seed: int) -> list[dict]:
    rng = random.Random(seed)
    p_base_model = load_fitted_p_base()
    invoices = []
    for i in range(n):
        family, class_ = ALL_CLASSES[i % len(ALL_CLASSES)]
        amount_paise = _sample_amount_paise(rng)
        invoices.append({
            "debtor_id": f"dryrun_debtor_{i:05d}",
            "invoice_id": f"DRYRUN-INV-{i:05d}",
            "amount_paise": amount_paise,
            "p_base": p_base_model.predict(amount_paise),
            "diagnosis": ExtractionResult(family=family, **{"class": class_}, confidence=round(rng.uniform(0.6, 1.0), 2)),
        })
    return invoices


def run_batch(invoices: list[dict]) -> list[dict]:
    ledger = Ledger(str(LEDGER_PATH))
    outbound_store = OutboundActionStore(str(OUTBOUND_PATH))
    rail = SimulatedRail(webhook_secret="dry-run-batch")  # never called under dry_run -- see module docstring
    results = []
    try:
        for inv in invoices:
            result = run_pipeline(
                debtor_id=inv["debtor_id"], invoice_id=inv["invoice_id"], amount_paise=inv["amount_paise"],
                diagnosis=inv["diagnosis"], channel_tag="telegram", ledger=ledger, outbound_store=outbound_store,
                rail=rail, dry_run=True,
            )
            results.append({
                "debtor_id": inv["debtor_id"], "invoice_id": inv["invoice_id"], "amount_paise": inv["amount_paise"],
                "family": inv["diagnosis"].family.value, "class": inv["diagnosis"].class_.value,
                "action_type": result.action_type.value, "ev_paise": result.ev_paise,
                "bounds_passed": result.bounds_passed, "refusal_reasons": result.refusal_reasons,
                "dry_run": result.action_outcome.dry_run if result.action_outcome else None,
                "external_ref": result.action_outcome.external_ref if result.action_outcome else None,
            })
        ledger.verify_chain()
        chain_ok = True
    finally:
        ledger.close()
        outbound_store.close()
        rail_links_created = len(rail._links) + len(rail._invoices) + len(rail._mandates)
    assert rail_links_created == 0, "dry_run must never touch the rail -- this is the whole point of this tool"
    return results, chain_ok


def render_markdown(results: list[dict], *, chain_ok: bool) -> str:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = len(results)
    passed = sum(1 for r in results if r["bounds_passed"])
    refused = n - passed
    external_refs = sum(1 for r in results if r["external_ref"] is not None)
    action_counts = Counter(r["action_type"] for r in results)
    family_counts = Counter(r["family"] for r in results)

    lines = [
        "# Dry-Run Batch — real decisions, zero rupees moved",
        "",
        f"Generated by `tools/run_dry_run_batch.py` at {now}. {n} synthetic invoices, spanning all "
        "29 real DiagnosisClass values across all four families, pushed through the actual pipeline "
        "(`agent.orchestrate.run_pipeline`) with `dry_run=True` — real DECIDE (fitted `p_base`, real "
        "`compute_ev()`), real BOUNDS (`check_bounds()`), and ACT logging what it would have "
        f"dispatched instead of dispatching it.",
        "",
        f"**{n} decisions made. {external_refs} rail calls made (must be 0). Ledger chain "
        f"verified: {chain_ok}.**",
        "",
        f"- BOUNDS passed: {passed} ({passed/n:.1%})",
        f"- BOUNDS refused: {refused} ({refused/n:.1%})",
        "",
        "## Action distribution",
        "",
        "| Action | Count |",
        "|---|---|",
    ]
    for action, count in action_counts.most_common():
        lines.append(f"| `{action}` | {count} |")
    lines.append("")
    lines.append("## Family distribution")
    lines.append("")
    lines.append("| Family | Count |")
    lines.append("|---|---|")
    for family, count in sorted(family_counts.items()):
        lines.append(f"| {family} | {count} |")
    lines.append("")
    lines.append(
        "Every row above came from a real `run_pipeline()` call — the same function the live "
        "webhook orchestrator (`agent/api/app.py`) and the real batch run "
        "(`tools/run_real_scenarios.py`) both call. The only difference here is `dry_run=True`: "
        "`execute_action()` never claims the outbound idempotency key and never calls the rail "
        "(`agent/act/executor.py`) — proven, not asserted: this script asserts zero rail objects "
        "were created before writing anything out."
    )
    lines.append("")
    if refused == 0:
        lines.append(
            "**Honest note on the 0% refusal rate**: this batch is structurally incapable of "
            "triggering a bounds refusal — every invoice has a unique, first-time debtor "
            "(`touches_7d=0` for all of them, so `TOUCH_BUDGET` can never fire), and "
            "`run_pipeline()`'s own signature has no parameter for a disputed amount, so "
            "`DISPUTE_FREEZE`/`NO_MANDATE_ON_DISPUTE` can't be exercised through this path "
            "either. `tools/run_real_scenarios.py` (the *real*, non-dry-run batch) deliberately "
            "includes a scenario that **is** refused, against the real rail — see "
            "`docs/evidence/REAL_SCENARIOS.md` for BOUNDS actually saying no to something. This "
            "batch's job is breadth across the diagnosis taxonomy and proving zero money moves, "
            "not proving the gate refuses (that's the other evidence file's job)."
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=500)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    invoices = generate_batch(args.n, seed=args.seed)
    results, chain_ok = run_batch(invoices)

    suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    json_path = EVIDENCE_DIR / f"dry_run_batch_{suffix}.json"
    json_path.write_text(json.dumps({"n": args.n, "seed": args.seed, "chain_verified": chain_ok, "results": results}, indent=2), encoding="utf-8")

    markdown = render_markdown(results, chain_ok=chain_ok)
    (EVIDENCE_DIR / "DRY_RUN_BATCH.md").write_text(markdown, encoding="utf-8")

    print(f"{args.n} decisions made, 0 rail calls, chain verified: {chain_ok}")
    print(f"Written to {EVIDENCE_DIR / 'DRY_RUN_BATCH.md'} and {json_path}")


if __name__ == "__main__":
    main()
