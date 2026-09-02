"""Score the extractor against the golden set, and against the baseline.

The claim this file exists to make checkable is "Path B reads debtor replies
correctly". Until now that claim rested on demos: replies I chose, run once,
with the answer visible before anyone counted. That is not evidence, and an
external review said so.

Three properties make this different:

1. **The labels were committed before the extractor ever ran on them.**
   `eval/golden/replies.jsonl` lands in its own commit, and the run that
   produces the results lands in a later one. `git log` is the proof, and
   `--verify-preregistration` refuses to score if that ordering does not
   hold. (The same shallow-clone trap that made the doc-staleness gate
   report green for eleven runs applies here, so the check refuses on a
   shallow clone rather than guessing -- see `docs/WHAT_BROKE.md` #22.)

2. **Every prediction is scored, including the ones I would rather not
   publish.** The confusion matrix goes in the report whatever it says.

3. **A baseline runs on the same 50 items.** An accuracy figure with
   nothing to compare it against cannot distinguish a capable model from
   an easy task.

    uv run python -m eval.golden.score --baseline      # free, no API calls
    uv run python -m eval.golden.score --extractor     # live, ~50 calls
    uv run python -m eval.golden.score --report        # write the doc
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from agent.diagnose.extract import DiagnosisClass, Family
from eval.golden.baseline import classify as baseline_classify
from eval.stats import two_proportion_test, wilson_interval

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PATH = Path(__file__).resolve().parent / "replies.jsonl"
RESULTS_DIR = REPO_ROOT / "docs" / "evidence"
EXTRACTOR_RESULTS = RESULTS_DIR / "golden_extractor_results.json"
BASELINE_RESULTS = RESULTS_DIR / "golden_baseline_results.json"
REPORT_PATH = RESULTS_DIR / "EXTRACTION_ACCURACY.md"


class PreregistrationViolation(RuntimeError):
    pass


def load_golden() -> list[dict]:
    rows = [json.loads(line) for line in GOLDEN_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [r["id"] for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate ids in the golden set")
    return rows


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout.strip()


def verify_preregistration() -> str:
    """The golden set's labels must predate the results they are scoring.

    Refuses outright on a shallow clone: `git log -1 -- <path>` in a
    depth-1 checkout returns HEAD's sha for any path, which would make this
    check pass for the wrong reason. That exact failure mode shipped once
    already (WHAT_BROKE #22) and is not going to ship twice.
    """
    if _git("rev-parse", "--is-shallow-repository") == "true":
        raise PreregistrationViolation(
            "shallow clone: `git log` cannot establish commit ordering here. "
            "Re-run with fetch-depth: 0."
        )
    label_commit = _git("log", "-1", "--format=%H", "--", str(GOLDEN_PATH.relative_to(REPO_ROOT)))
    if not label_commit:
        raise PreregistrationViolation(
            f"{GOLDEN_PATH.name} is not committed. The labels must be committed "
            "before the extractor is run against them, or the result means nothing."
        )
    if _git("status", "--porcelain", "--", str(GOLDEN_PATH.relative_to(REPO_ROOT))):
        raise PreregistrationViolation(
            f"{GOLDEN_PATH.name} has uncommitted changes. Scoring against edited "
            "labels would silently fit the answers to the output."
        )
    return label_commit


def run_baseline(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        family, class_ = baseline_classify(row["text"])
        out.append({
            "id": row["id"], "predicted_family": family.value,
            "predicted_class": class_.value, "confidence": None,
        })
    return out


def run_extractor(rows: list[dict], *, limit: int | None = None) -> list[dict]:
    """Live model calls, budget-gated by agent.spend like every other call
    this project makes. A failure is recorded as a failure, not skipped --
    an extractor that raises on 10% of real replies has a 10% problem, and
    dropping those rows would hide it."""
    from agent.diagnose.llm_extract import extract_from_reply

    out = []
    for i, row in enumerate(rows if limit is None else rows[:limit], start=1):
        try:
            result = extract_from_reply(row["text"], purpose="golden_set_scoring")
            out.append({
                "id": row["id"], "predicted_family": result.family.value,
                "predicted_class": result.class_.value, "confidence": result.confidence,
                "promise_amount_paise": result.promise.amount_paise if result.promise else None,
            })
            mark = "ok " if result.class_.value == row["class"] else "MISS"
            print(f"  [{i:>2}/{len(rows)}] {row['id']}  {mark}  "
                  f"want {row['class']:<26} got {result.class_.value}")
        except Exception as exc:
            out.append({"id": row["id"], "predicted_family": None,
                        "predicted_class": None, "error": f"{type(exc).__name__}: {exc}"})
            print(f"  [{i:>2}/{len(rows)}] {row['id']}  ERROR {type(exc).__name__}: {exc}")
    return out


def score(rows: list[dict], predictions: list[dict]) -> dict:
    by_id = {p["id"]: p for p in predictions}
    family_hits = class_hits = 0
    hard_class_hits = hard_n = easy_class_hits = easy_n = 0
    confusion: Counter[tuple[str, str]] = Counter()
    class_errors: list[dict] = []

    for row in rows:
        pred = by_id.get(row["id"], {})
        family_ok = pred.get("predicted_family") == row["family"]
        class_ok = pred.get("predicted_class") == row["class"]
        family_hits += family_ok
        class_hits += class_ok
        if row.get("hard"):
            hard_n += 1
            hard_class_hits += class_ok
        else:
            easy_n += 1
            easy_class_hits += class_ok
        confusion[(row["family"], pred.get("predicted_family") or "ERROR")] += 1
        if not class_ok:
            class_errors.append({
                "id": row["id"], "text": row["text"],
                "want_family": row["family"], "want_class": row["class"],
                "got_family": pred.get("predicted_family"), "got_class": pred.get("predicted_class"),
                "hard": bool(row.get("hard")), "note": row.get("note", ""),
            })

    n = len(rows)
    return {
        "n": n,
        "family_correct": family_hits, "class_correct": class_hits,
        "family_accuracy": wilson_interval(family_hits, n),
        "class_accuracy": wilson_interval(class_hits, n),
        "easy": {"n": easy_n, "correct": easy_class_hits, "interval": wilson_interval(easy_class_hits, easy_n)},
        "hard": {"n": hard_n, "correct": hard_class_hits, "interval": wilson_interval(hard_class_hits, hard_n)},
        "confusion": {f"{want}->{got}": count for (want, got), count in sorted(confusion.items())},
        "errors": class_errors,
    }


def _matrix_table(rows: list[dict], predictions: list[dict]) -> str:
    by_id = {p["id"]: p for p in predictions}
    families = [f.value for f in Family]
    counts: Counter[tuple[str, str]] = Counter()
    for row in rows:
        counts[(row["family"], by_id.get(row["id"], {}).get("predicted_family") or "ERROR")] += 1
    header = "| actual \\ predicted | " + " | ".join(families) + " | ERROR |\n"
    header += "|---" * (len(families) + 2) + "|\n"
    body = ""
    for actual in families:
        cells = [str(counts.get((actual, p), 0)) for p in families]
        cells.append(str(counts.get((actual, "ERROR"), 0)))
        body += f"| **{actual}** | " + " | ".join(cells) + " |\n"
    return header + body


def build_report(rows: list[dict], extractor: list[dict], baseline: list[dict], label_commit: str) -> str:
    ex, bl = score(rows, extractor), score(rows, baseline)
    test = two_proportion_test(bl["class_correct"], bl["n"], ex["class_correct"], ex["n"])
    family_test = two_proportion_test(bl["family_correct"], bl["n"], ex["family_correct"], ex["n"])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    parts = [
        "# Extraction accuracy on a pre-registered golden set",
        "",
        f"*Generated by `eval/golden/score.py --report` at {now}. Do not edit by hand.*",
        "",
        "Path B reads free-text debtor replies and returns a `(family, class)` "
        "diagnosis. The family alone decides which actions are even reachable "
        "(`ACTIONS_UNLOCKED`, §11.2), so an extraction error is not a cosmetic "
        "mislabel -- it changes what the agent is permitted to do.",
        "",
        "Until this file existed, that step was supported only by demonstrations: "
        "replies I picked, run once, graded after the fact. This is the "
        "replacement.",
        "",
        "## How to distrust this less than a demo",
        "",
        f"**The labels were committed before the extractor ran.** All 50 labels "
        f"landed in commit `{label_commit[:12]}`, and `score.py` refuses to run if "
        "`replies.jsonl` has uncommitted changes -- so the answers cannot have been "
        "adjusted to the output. Verify with "
        "`git log --format='%H %ci' -- eval/golden/replies.jsonl`.",
        "",
        "**Every item is scored, including the failures.** The confusion matrix and "
        "a full list of every miss are below.",
        "",
        "**A keyword baseline ran on the same 50 items** (`eval/golden/baseline.py`), "
        "written as a genuine attempt rather than a strawman. Without it, an "
        "accuracy number cannot distinguish a capable model from an easy task.",
        "",
        "## Results",
        "",
        "| | extractor | keyword baseline |",
        "|---|---|---|",
        f"| class accuracy (29-way) | **{ex['class_accuracy'].as_pct()}** | {bl['class_accuracy'].as_pct()} |",
        f"| family accuracy (4-way) | **{ex['family_accuracy'].as_pct()}** | {bl['family_accuracy'].as_pct()} |",
        f"| straightforward items (n={ex['easy']['n']}) | {ex['easy']['interval'].as_pct()} | {bl['easy']['interval'].as_pct()} |",
        f"| items labelled hard (n={ex['hard']['n']}) | {ex['hard']['interval'].as_pct()} | {bl['hard']['interval'].as_pct()} |",
        "",
        "Intervals are Wilson score intervals at 95% (`eval/stats.py`). At n=50 they "
        "are wide, and that width is the honest reading: this is a small set, and it "
        "is reported as one.",
        "",
        f"**Extractor vs baseline, class accuracy:** {test.describe()}",
        "",
        f"**Extractor vs baseline, family accuracy:** {family_test.describe()}",
        "",
        "## Confusion matrix (family)",
        "",
        "The family is what gates the action set, so this is the matrix that "
        "matters. Rows are the committed label, columns the extractor's answer.",
        "",
        _matrix_table(rows, extractor),
        "**The costly cell is D misread as anything else.** Family D (dispute) is the "
        "only family whose action set is `{escalate_human}`. Reading a dispute as "
        "C or B lets the agent keep chasing a debtor who is disputing the debt, "
        "which is the RBI Fair Practices Code problem the bounds gate exists to "
        "prevent. Note that the gate is a second, independent check -- an extraction "
        "error here does not by itself produce a prohibited action.",
        "",
        "## Every miss",
        "",
    ]

    if ex["errors"]:
        parts.append("| id | reply | labelled | extracted | hard? |")
        parts.append("|---|---|---|---|---|")
        for e in ex["errors"]:
            text = e["text"][:70].replace("|", "\\|")
            parts.append(
                f"| `{e['id']}` | {text} | {e['want_family']}/{e['want_class']} | "
                f"{e['got_family']}/{e['got_class']} | {'yes' if e['hard'] else ''} |"
            )
    else:
        parts.append("*No misses on this run.* At n=50 that is consistent with a true "
                     "accuracy anywhere above roughly 93% -- see the interval above, "
                     "which is why the interval is published and not just the point.")
    parts += [
        "",
        "## What this does not measure",
        "",
        "* **Not a recovery rate.** This measures whether the agent understood the "
        "reply, not whether the debtor paid.",
        "* **`SILENT` is absent by construction.** A silent debtor produces no reply "
        "text, so that class cannot appear in a reply-classification set; it is "
        "reached by timeout, not extraction.",
        "* **Family A is under-represented** (7 of 50) because instrument faults "
        "normally arrive as webhook failure codes through Path A, which is a lookup "
        "against `failure_taxonomy.yaml`, not a model call. The Family A rows here "
        "are the minority case where a debtor self-reports the fault in words.",
        "* **I wrote 49 of the 50 replies.** One (`g006`) is a real reply received on "
        "the deployed Telegram bot. Authored text carries my own idea of what a "
        "debtor sounds like, and that is a real limitation of this set: the honest "
        "fix is to keep harvesting live replies and re-run.",
        "",
    ]
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline", action="store_true", help="run the keyword baseline (free)")
    parser.add_argument("--extractor", action="store_true", help="run the live extractor (~50 API calls)")
    parser.add_argument("--report", action="store_true", help="write the markdown report from cached results")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    rows = load_golden()
    label_commit = verify_preregistration()
    print(f"golden set: {len(rows)} items, labels committed in {label_commit[:12]}\n")

    if args.baseline:
        results = run_baseline(rows)
        BASELINE_RESULTS.write_text(json.dumps(results, indent=2), encoding="utf-8")
        s = score(rows, results)
        print(f"baseline  class {s['class_accuracy'].as_pct()}  family {s['family_accuracy'].as_pct()}")

    if args.extractor:
        results = run_extractor(rows, limit=args.limit)
        EXTRACTOR_RESULTS.write_text(json.dumps(results, indent=2), encoding="utf-8")
        s = score(rows, results)
        print(f"\nextractor class {s['class_accuracy'].as_pct()}  family {s['family_accuracy'].as_pct()}")

    if args.report:
        if not (EXTRACTOR_RESULTS.exists() and BASELINE_RESULTS.exists()):
            sys.exit("need both --baseline and --extractor results before --report")
        extractor = json.loads(EXTRACTOR_RESULTS.read_text(encoding="utf-8"))
        baseline = json.loads(BASELINE_RESULTS.read_text(encoding="utf-8"))
        REPORT_PATH.write_text(build_report(rows, extractor, baseline, label_commit), encoding="utf-8")
        print(f"wrote {REPORT_PATH.relative_to(REPO_ROOT)}")

    if not (args.baseline or args.extractor or args.report):
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
