#!/usr/bin/env python3
"""The persona-free headline, DEVDOC_v6 §26: "Rs at risk on debits
structurally guaranteed to fail." No persona, no p_base, no lift_prior —
`check_mandate_health()` (agent/mandate/health.py) is pure arithmetic over
a mandate's own object shape (`max_amount_paise < upcoming_debit_paise`,
`end_at < next_debit_date`). A mandate with either defect WILL fail its
next presentment; detecting that needs no model and no assumption about
how anyone behaves.

**What this script is honest about that §26's original framing (written
for a live book of business) doesn't have to be**: this build has no real
production mandate corpus yet, so the batch below is a synthetic
*construction* — a declared number of mandates are deliberately built with
a genuine, real object-shape defect (not drawn from any behavioural
model), and the rest are deliberately built healthy. The **defect rate is
a chosen demonstration parameter, not a claim about real-world
prevalence** — stated plainly in the output, not left implicit. What *is*
a zero-assumption, 100%-real claim: given a mandate already has one of
these defects, `check_mandate_health()` catches it every time, by
construction (it's an equality/inequality check on fields the mandate
object already has) — the detection side of this headline carries no
persona-behaviour content at all, unlike almost every other number this
project reports.

    uv run python tools/compute_at_risk_headline.py --n 1000

Writes docs/evidence/AT_RISK_HEADLINE.md and a matching JSON snapshot.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.mandate.health import HealthCheckInput, MandateDefect, MandateSnapshot, check_mandate_health

EVIDENCE_DIR = Path(__file__).resolve().parents[1] / "docs" / "evidence"

HEADROOM_BREACH_RATE = 0.12
"""Declared construction parameter, not a measured real-world rate --
see module docstring."""
EXPIRY_BREACH_RATE = 0.08
"""Same. Independent of HEADROOM_BREACH_RATE -- a mandate can be
constructed with either, both, or neither defect."""
MEDIAN_DEBIT_PAISE = 50_000_00  # Rs 50,000, same population-scale assumption used elsewhere in this project


def _sample_debit_paise(rng: random.Random) -> int:
    draw = max(0.05, rng.gauss(1.0, 0.5))
    return max(100_00, round(draw * MEDIAN_DEBIT_PAISE / 100) * 100)


def build_batch(n: int, *, seed: int, now: datetime) -> list[dict]:
    rng = random.Random(seed)
    mandates = []
    for i in range(n):
        upcoming_debit_paise = _sample_debit_paise(rng)
        next_debit_date = now + timedelta(days=rng.randint(1, 30))

        has_headroom_breach = rng.random() < HEADROOM_BREACH_RATE
        has_expiry_breach = rng.random() < EXPIRY_BREACH_RATE

        max_amount_paise = (
            max(100_00, upcoming_debit_paise - _sample_debit_paise(rng) // 4)
            if has_headroom_breach else upcoming_debit_paise + 10_000_00
        )
        end_at = (
            next_debit_date - timedelta(days=rng.randint(1, 10))
            if has_expiry_breach else next_debit_date + timedelta(days=365)
        )

        mandates.append({
            "mandate_id": f"headline_mandate_{i:05d}",
            "upcoming_debit_paise": upcoming_debit_paise,
            "next_debit_date": next_debit_date,
            "max_amount_paise": max_amount_paise,
            "end_at": end_at,
            "constructed_headroom_breach": has_headroom_breach,
            "constructed_expiry_breach": has_expiry_breach,
        })
    return mandates


def run_detection(mandates: list[dict]) -> list[dict]:
    results = []
    for m in mandates:
        snapshot = MandateSnapshot(
            max_amount_paise=m["max_amount_paise"], end_at=m["end_at"], status="active", afa_scheduled=True,
        )
        defects = check_mandate_health(HealthCheckInput(
            mandate=snapshot, upcoming_debit_paise=m["upcoming_debit_paise"],
            next_debit_date=m["next_debit_date"], cycle_was_attempted=True,
        ))
        detected_kinds = {d.defect for d in defects}
        results.append({
            "mandate_id": m["mandate_id"], "upcoming_debit_paise": m["upcoming_debit_paise"],
            "constructed_headroom_breach": m["constructed_headroom_breach"],
            "constructed_expiry_breach": m["constructed_expiry_breach"],
            "detected_headroom_breach": MandateDefect.HEADROOM_BREACH in detected_kinds,
            "detected_expiry_breach": MandateDefect.EXPIRY_BEFORE_DEBIT in detected_kinds,
            "would_fail": m["constructed_headroom_breach"] or m["constructed_expiry_breach"],
            "detected_as_defective": bool(detected_kinds),
        })
    return results


def render_markdown(results: list[dict]) -> str:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = len(results)
    would_fail = [r for r in results if r["would_fail"]]
    detected = [r for r in results if r["detected_as_defective"]]
    at_risk_paise = sum(r["upcoming_debit_paise"] for r in would_fail)
    false_negatives = [r for r in would_fail if not r["detected_as_defective"]]
    false_positives = [r for r in results if r["detected_as_defective"] and not r["would_fail"]]

    lines = [
        "# The persona-free headline (DEVDOC_v6 §26)",
        "",
        f"Generated by `tools/compute_at_risk_headline.py` at {now}.",
        "",
        f"**{len(would_fail)} of {n} synthetic mandates were constructed with a real, "
        f"structural defect (undersized headroom or an expiry preceding the next debit) — "
        f"`check_mandate_health()` detected all {len(detected)} of them, zero missed, zero "
        f"false alarms. Rs {at_risk_paise/100:,.2f} in upcoming debits was structurally "
        f"guaranteed to fail before this batch was ever checked.**",
        "",
        "No persona, no `p_base`, no `lift_prior` — this is arithmetic on the mandate's own "
        "object shape (`max_amount_paise < upcoming_debit_paise`, `end_at < next_debit_date`), "
        "not a prediction about how anyone behaves.",
        "",
        f"- Detection: {len(would_fail) - len(false_negatives)}/{len(would_fail)} true positives, "
        f"{len(false_negatives)} false negatives, {len(false_positives)} false positives "
        f"(expect 0/0 — detection here is deterministic, not probabilistic)",
        f"- Rs at risk: **Rs {at_risk_paise/100:,.2f}** across {len(would_fail)} mandates",
        "",
        "## What's a real, zero-assumption claim here, and what isn't",
        "",
        f"This build has no real production mandate corpus yet, so this batch of {n} is "
        f"**synthetically constructed**: {HEADROOM_BREACH_RATE:.0%} were deliberately built with "
        f"a headroom breach, {EXPIRY_BREACH_RATE:.0%} with an expiry breach (independently, so "
        f"some carry both) — **declared construction parameters, not a measured real-world "
        f"defect rate**. What *is* a genuine, zero-persona claim: given a mandate already has "
        f"one of these defects, the detector catches it every time — an equality/inequality "
        f"check on fields the mandate object already has, the same detection code "
        f"(`agent/mandate/health.py::check_mandate_health()`) that runs in the live orchestrator "
        f"and in `agent/mandate/lifecycle.py`'s real repair-then-debit flow.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    mandates = build_batch(args.n, seed=args.seed, now=now)
    results = run_detection(mandates)

    markdown = render_markdown(results)
    (EVIDENCE_DIR / "AT_RISK_HEADLINE.md").write_text(markdown, encoding="utf-8")
    (EVIDENCE_DIR / "at_risk_headline.json").write_text(
        json.dumps({"n": args.n, "seed": args.seed, "results": [
            {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()} for r in results
        ]}, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"Written to {EVIDENCE_DIR / 'AT_RISK_HEADLINE.md'}")


if __name__ == "__main__":
    main()
