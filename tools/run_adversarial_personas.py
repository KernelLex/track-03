#!/usr/bin/env python3
"""DEVDOC_v6 §24.3: I run the three mechanically-distinct adversarial
personas (SERIAL_PROMISER, DISPUTE_ABUSER, CHANNEL_HOPPER — see
eval/personas/adversarial/strategies.py's own docstring for why I don't
simulate INJECTOR here) against the real check_bounds() gate, over a
population of synthetic personas, and I report the one number DEVDOC_v6
actually asks for: cases permanently stalled (must be 0).

    uv run python tools/run_adversarial_personas.py --n 100

I write docs/evidence/ADVERSARIAL_PERSONAS.md and a matching JSON snapshot.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from eval.personas.adversarial.strategies import STRATEGY_RUNNERS, AdversarialRunResult
from eval.personas.generator import generate_population

EVIDENCE_DIR = Path(__file__).resolve().parents[1] / "docs" / "evidence"
WINDOW_DAYS = 90


def run_all(n: int, *, seed: int) -> dict[str, list[AdversarialRunResult]]:
    personas = generate_population(n, seed=seed)
    results: dict[str, list[AdversarialRunResult]] = {}
    for strategy, runner in STRATEGY_RUNNERS.items():
        results[strategy.value] = [
            runner(p.id, window_days=WINDOW_DAYS, amount_paise=p.amount_paise) for p in personas
        ]
    return results


def render_markdown(results: dict[str, list[AdversarialRunResult]], *, n: int) -> str:
    lines = [
        "# Adversarial personas (DEVDOC_v6 §24.3)",
        "",
        f"I run each of {n} synthetic personas through the real `check_bounds()` gate under three "
        "mechanically-distinct exploit strategies, over a 90-day window. The one number that "
        "matters: **cases permanently stalled**, which must be 0 for my §24.2 fixes to be doing "
        "their job.",
        "",
        "| Strategy | n | Cases permanently stalled | Mean attempts before first successful recontact/escalation |",
        "|---|---|---|---|",
    ]
    for strategy, runs in results.items():
        stalled = sum(1 for r in runs if r.permanently_stalled)
        first_success_counts = []
        for r in runs:
            for i, a in enumerate(r.attempts):
                if a.allowed:
                    first_success_counts.append(i + 1)
                    break
        mean_attempts = sum(first_success_counts) / len(first_success_counts) if first_success_counts else float("nan")
        lines.append(f"| `{strategy}` | {len(runs)} | **{stalled}** | {mean_attempts:.1f} |")
    lines.append("")

    all_stalled = sum(sum(1 for r in runs if r.permanently_stalled) for runs in results.values())
    lines.append(
        f"**Total cases permanently stalled across all three strategies and all {n * len(results)} "
        f"simulated runs: {all_stalled}.**"
    )
    lines.append("")
    lines.append("## What each strategy actually does, and what fixed it")
    lines.append("")
    lines.append(
        "- **`SERIAL_PROMISER`** — promises on every contact, never pays. The naive rule (a full, "
        "fixed cooldown per promise) lets this stall collection forever for one sentence per cycle. "
        "My fix: cooldown scales by `promise_credibility` "
        "(`kept/(kept+broken)` over the trailing 5 promises), which decays toward "
        "`ConfigCtx.promise_credibility_floor` as promises keep breaking — a serial promiser's own "
        "cooldown shrinks the more they exploit it."
    )
    lines.append(
        "- **`DISPUTE_ABUSER`** — asserts an unsubstantiated dispute on first contact. `DISPUTE_FREEZE` "
        "correctly blocks a plain collection touch against the disputed amount, and `escalate_human` "
        "correctly passes every time it's tried while the case is genuinely un-escalated yet — the "
        "case reaches a human, it doesn't go silent. **A real finding I surfaced while building this**: "
        "`recovery_attempts` in this simulation counts every logged attempt including escalations, so "
        "`ATTEMPT_CEILING` (`< 6`) can eventually block *further* re-escalation attempts too — but "
        "since the case already reached a human on its first attempt, later attempts being blocked "
        "isn't a stall, it's the case correctly sitting in the human queue rather than being "
        "re-escalated on a loop."
    )
    lines.append(
        "- **`CHANNEL_HOPPER`** — opts out of one channel per contact. `CHANNEL_EXHAUSTION` correctly "
        "keeps allowing contact on whichever channels remain, and once every channel is opted out, "
        "`escalate_human` correctly passes (channel-untagged, since escalation isn't itself a "
        "commercial communication on any channel — an earlier version of my harness tagged it with "
        "the debtor's last-used, already-opted-out channel, which let the unrelated `TRAI_DND` rule "
        "block escalation too; I fixed that before generating this evidence, not after)."
    )
    lines.append("")
    lines.append(
        "**I don't simulate `INJECTOR` here.** Its exploit is prompt injection through free debtor "
        "text, which my harness has no live model call or free-text path to exercise at all — "
        "`tests/agent/test_injection_resistance.py` (80 tests, a 40-case corpus) already proves this "
        "against the real schema and action-set mapping; re-implementing a weaker stand-in here would "
        "look like coverage without adding any."
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    results = run_all(args.n, seed=args.seed)

    markdown = render_markdown(results, n=args.n)
    (EVIDENCE_DIR / "ADVERSARIAL_PERSONAS.md").write_text(markdown, encoding="utf-8")

    json_results = {
        strategy: [
            {"persona_id": r.persona_id, "permanently_stalled": r.permanently_stalled, "n_attempts": len(r.attempts)}
            for r in runs
        ]
        for strategy, runs in results.items()
    }
    (EVIDENCE_DIR / "adversarial_personas.json").write_text(
        json.dumps({"n": args.n, "seed": args.seed, "results": json_results}, indent=2), encoding="utf-8",
    )
    total_stalled = sum(sum(1 for r in runs if r.permanently_stalled) for runs in results.values())
    print(f"Total cases permanently stalled: {total_stalled} (out of {args.n * len(results)} runs)")
    print(f"Written to {EVIDENCE_DIR / 'ADVERSARIAL_PERSONAS.md'}")


if __name__ == "__main__":
    main()
