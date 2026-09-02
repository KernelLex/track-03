#!/usr/bin/env python3
"""A real batch: N agent decisions that each create a real Razorpay object.

This is the one thing `run_dry_run_batch.py` deliberately cannot do. That
tool proves the *judgment* at n=500 with zero rupees moved; this one proves
the *pipeline* end to end at small n, on the live (test-mode) rail, with
`dry_run=False` -- the agent decides, `check_bounds()` gates, ACT calls
Razorpay, and a human can then pay the resulting invoice so a webhook
attributes the capture back through INGEST -> SETTLE.

**What this measures, and what it does not.** It measures pipeline
completeness: decision -> gate -> real rail object -> real capture ->
attributed recovery. It is *not* a recovery rate. Ten invoices paid by the
person who ran the batch says nothing about whether debtors pay; only the
counterfactual arms in `docs/RESULTS.md` speak to that, and they are
simulated. Anyone reading a percentage into this file is reading it wrong.

**Why Family B.** Two honest constraints meet here:

  * Family B (artifact defects: wrong PO, missing GST, invoice never
    arrived) is the family whose correct repair genuinely *is* reissuing
    the document -- `_decide_next_step` maps it to `REISSUE_ARTIFACT`.
  * `REISSUE_ARTIFACT` calls `rail.create_invoice()`, which this account
    can still do. `CREATE_PAYMENT_LINK` cannot: the account's payment-link
    allowance counts lifetime creates and is exhausted (`docs/
    WHAT_BROKE.md`), so a Family A batch would measure Razorpay's quota,
    not this pipeline.

So the *population* is chosen; the *decision* is not. EV is computed by the
fitted model, the gate can still refuse, and whatever the agent picks is
what gets executed and recorded -- including a refusal, and including a
rail error.

    uv run python tools/run_real_batch.py --n 10          # create
    uv run python tools/run_real_batch.py --report        # after paying

Creates real objects on a real account. Requires --yes to run unattended.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agent.act.executor import OutboundActionStore  # noqa: E402
from agent.decide.fitted_p_base import load_fitted_p_base  # noqa: E402
from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family  # noqa: E402
from agent.ledger.store import Ledger  # noqa: E402
from agent.orchestrate import run_pipeline  # noqa: E402
from agent.rails.razorpay_rail import RazorpayRail  # noqa: E402

EVIDENCE_DIR = REPO_ROOT / "docs" / "evidence"
LEDGER_PATH = EVIDENCE_DIR / "real_batch_ledger.db"
OUTBOUND_PATH = EVIDENCE_DIR / "real_batch_outbound.db"
STATE_PATH = EVIDENCE_DIR / "real_batch_state.json"

# The seven Family B classes, in taxonomy order. A batch of 10 cycles this
# list, so classes 0-2 appear twice -- recorded per row rather than hidden.
FAMILY_B_CLASSES = [
    DiagnosisClass.INVOICE_NOT_RECEIVED,
    DiagnosisClass.PO_MISMATCH,
    DiagnosisClass.GST_DEFECT,
    DiagnosisClass.APPROVAL_BOTTLENECK,
    DiagnosisClass.DOCUMENT_MISSING,
    DiagnosisClass.BANK_DETAIL_MISMATCH,
    DiagnosisClass.ALREADY_PAID_UNRECONCILED,
]

MEDIAN_AMOUNT_PAISE = 50_000_00


def _load_dotenv() -> None:
    """The other live tools require the caller to have exported credentials.
    This one loads .env if present, because a batch that half-runs and then
    dies on a missing key leaves real objects behind."""
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _client() -> RazorpayRail:
    key_id, key_secret = os.environ.get("RAZORPAY_KEY_ID"), os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        sys.exit("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET must be set (or present in .env)")
    if not key_id.startswith("rzp_test_"):
        sys.exit(f"refusing to run against a non-test key ({key_id[:11]}...) -- this tool creates real objects")
    return RazorpayRail(key_id=key_id, key_secret=key_secret)


def _sample_amount_paise(rng: random.Random) -> int:
    draw = max(0.05, rng.gauss(1.0, 0.6))
    return max(100_00, round(draw * MEDIAN_AMOUNT_PAISE / 100) * 100)


def build_batch(n: int, *, seed: int, run_tag: str) -> list[dict]:
    rng = random.Random(seed)
    p_base_model = load_fitted_p_base()
    rows = []
    for i in range(n):
        class_ = FAMILY_B_CLASSES[i % len(FAMILY_B_CLASSES)]
        amount_paise = _sample_amount_paise(rng)
        rows.append({
            "debtor_id": f"realbatch_{run_tag}_{i:02d}",
            "invoice_id": f"RB-{run_tag}-{i:02d}",
            "amount_paise": amount_paise,
            "p_base": p_base_model.predict(amount_paise),
            "diagnosis": ExtractionResult(
                family=Family.B, **{"class": class_},
                confidence=round(rng.uniform(0.66, 0.98), 2),
            ),
        })
    return rows


SPACING_SECONDS = 2.0
"""Razorpay rate-limits invoice creation. The first run of this tool fired
ten creates back to back: five succeeded and five came back `BadRequestError:
Too many requests` (docs/WHAT_BROKE.md #28). No test caught it because
SimulatedRail has no rate limit to hit.

Two seconds between creates, plus the retry below. Deliberately a plain
sleep rather than anything adaptive -- a batch of ten is not worth a token
bucket, and an honest 20-second run beats a clever one that fails at n=50."""

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5.0


def _is_rate_limit(exc: Exception) -> bool:
    return "too many requests" in str(exc).lower()


def run_batch(rows: list[dict], *, rail: RazorpayRail) -> tuple[list[dict], bool]:
    ledger = Ledger(str(LEDGER_PATH))
    outbound_store = OutboundActionStore(str(OUTBOUND_PATH))
    results = []
    try:
        for index, row in enumerate(rows):
            if index:
                time.sleep(SPACING_SECONDS)
            record = {
                "debtor_id": row["debtor_id"], "invoice_id": row["invoice_id"],
                "amount_paise": row["amount_paise"],
                "class": row["diagnosis"].class_.value,
                "confidence": row["diagnosis"].confidence,
            }
            result, failure = None, None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    result = run_pipeline(
                        debtor_id=row["debtor_id"], invoice_id=row["invoice_id"],
                        amount_paise=row["amount_paise"], diagnosis=row["diagnosis"],
                        channel_tag="telegram", ledger=ledger, outbound_store=outbound_store,
                        rail=rail, dry_run=False,
                    )
                    break
                except Exception as exc:
                    failure = exc
                    # Only a rate limit is worth retrying. Anything else --
                    # a validation error, an exhausted quota -- will fail
                    # identically three times and just slow the batch down
                    # on its way to the same answer.
                    if not _is_rate_limit(exc) or attempt == MAX_RETRIES:
                        break
                    wait = RETRY_BACKOFF_SECONDS * attempt
                    print(f"  {record['invoice_id']}  rate-limited, retrying in {wait:.0f}s "
                          f"(attempt {attempt}/{MAX_RETRIES})")
                    time.sleep(wait)

            if result is None:
                # A rail error on one row must not strand the other nine as
                # half-created objects with no record of why.
                record.update({"error": f"{type(failure).__name__}: {failure}", "action_type": None})
                results.append(record)
                print(f"  {record['invoice_id']}  ERROR  {type(failure).__name__}: {failure}")
                continue

            outcome = result.action_outcome
            detail = (outcome.detail or {}) if outcome else {}
            record.update({
                "action_type": result.action_type.value,
                "ev_paise": result.ev_paise,
                "bounds_passed": result.bounds_passed,
                "refusal_reasons": result.refusal_reasons,
                "external_ref": outcome.external_ref if outcome else None,
                "short_url": detail.get("short_url"),
                "rail_status": detail.get("status"),
            })
            results.append(record)
            print(f"  {record['invoice_id']}  {record['class']:<26} "
                  f"{record['action_type']:<18} {record['external_ref'] or '-':<22} "
                  f"{'PASS' if record['bounds_passed'] else 'REFUSED ' + ','.join(record['refusal_reasons'] or [])}")
        ledger.verify_chain()
        chain_ok = True
    finally:
        ledger.close()
        outbound_store.close()
    return results, chain_ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20260902)
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    _load_dotenv()
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    if not args.yes:
        print(f"This creates {args.n} REAL Razorpay invoices on the configured test account.")
        if input("Type 'yes' to continue: ").strip().lower() != "yes":
            return 1

    rail = _client()
    run_tag = datetime.now(timezone.utc).strftime("%m%d%H%M")
    rows = build_batch(args.n, seed=args.seed, run_tag=run_tag)

    print(f"\nrunning {args.n} decisions with dry_run=False against {os.environ['RAZORPAY_KEY_ID'][:16]}...\n")
    results, chain_ok = run_batch(rows, rail=rail)

    payable = [r for r in results if r.get("short_url")]
    state = {
        "run_tag": run_tag,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "key_id": os.environ["RAZORPAY_KEY_ID"],
        "n": args.n, "seed": args.seed,
        "ledger_chain_verified": chain_ok,
        "results": results,
    }
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    raw_path = EVIDENCE_DIR / f"real_batch_{run_tag}.json"
    raw_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    executed = [r for r in results if r.get("external_ref")]
    refused = [r for r in results if r.get("bounds_passed") is False]
    errored = [r for r in results if r.get("error")]
    print(f"\n{len(executed)}/{args.n} created a real object   "
          f"{len(refused)} refused by the gate   {len(errored)} rail errors   "
          f"chain verified: {chain_ok}")
    print(f"state -> {STATE_PATH.relative_to(REPO_ROOT)}")

    if payable:
        print(f"\n--- {len(payable)} payable links (test card 4111 1111 1111 1111, any future expiry/CVV) ---")
        for r in payable:
            print(f"  Rs {r['amount_paise'] / 100:>10,.2f}  {r['short_url']}")
        print("\nPay whichever you like, then: uv run python tools/report_real_batch.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
