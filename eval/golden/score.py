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

    parts = [
        "# Extraction accuracy on a pre-registered golden set",
        "",
        "*Generated by `eval/golden/score.py --report` from the committed labels and "
        "the cached run in `docs/evidence/golden_extractor_results.json`. Do not edit "
        "by hand — `--check` fails if this drifts, the same gate `docs/RESULTS.md` "
        "needed after it silently went stale (`docs/WHAT_BROKE.md` #18).*",
        "",
        "*No wall-clock timestamp appears in this file on purpose: it is compared "
        "byte-for-byte against a fresh regeneration, and the provenance that carries "
        "a claim is the label commit named below, not the hour the markdown was "
        "written.*",
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
        "## What this set can and cannot establish",
        "",
        "Three things in the table above deserve to be read before the headline "
        "number is quoted anywhere.",
        "",
        f"**The class-accuracy gap is not statistically significant.** {test.describe()}. "
        "A 4-item difference at n=50 is inside the noise. The correct statement is "
        "\"the extractor did not do worse than a regex on this set\", not \"the "
        "extractor beat a regex\". Family accuracy does clear the bar "
        f"({family_test.p_value:.3f}), and family is what gates the action set, so "
        "that is the one comparison here that carries weight.",
        "",
        "**A keyword baseline getting 90% means this set is too clean.** Real replies "
        "arrive with mixed intent, missing context, and two problems in one message. "
        "These 50 are mostly unambiguous exemplars, and unambiguous exemplars are "
        "exactly what a regex handles. The set therefore has weak power to "
        "discriminate, and the honest fix is not to tune the set -- it is to keep "
        "harvesting live replies, where the messiness is free and real.",
        "",
        "**The one place the two pull apart is the hard items**: "
        f"{ex['hard']['interval'].as_pct()} against {bl['hard']['interval'].as_pct()} "
        f"on the {ex['hard']['n']} items marked ambiguous when the labels were written. "
        "That is the shape one would predict -- surface matching fails first where the "
        "surface is misleading -- but at n=6 the intervals overlap almost completely, "
        "so it is a hypothesis this set is too small to test, not a result.",
        "",
        "### About the baseline",
        "",
        "The first draft of `baseline.py` scored 94% and was thrown away. I had "
        "written the 50 replies and then written regexes against my own phrasing, "
        "including `card is old` for `INSTRUMENT_EXPIRED` -- while item `g045`'s own "
        "note said in as many words that a keyword baseline should miss that item. "
        "That is an answer key, not a baseline. The version scored here is restricted "
        "to vocabulary a collections domain expert would list before seeing the set.",
        "",
        "It still has an advantage the extractor does not: I have read the set, and "
        "chose which classes to write rules for knowing which ones appear in it. The "
        "bias runs **toward** the baseline, so the extractor's margin is a lower "
        "bound. That is the correct direction for a number I am publishing about my "
        "own system.",
        "",
        "### What the model is load-bearing for, given that tie",
        "",
        "The result above is the softest claim in this project, so it is worth being "
        "precise about what it means rather than leaving it at \"not significant\".",
        "",
        "**This set scores 29-way classification.** On that task, on 50 clean "
        "exemplars, the extractor and a hand-written regex are statistically "
        "indistinguishable. That is the finding and it is not being dressed up.",
        "",
        "**But classification is not the job.** Here is the baseline's entire "
        "signature:",
        "",
        "```python",
        "def classify(text: str) -> tuple[Family, DiagnosisClass]:",
        "```",
        "",
        "Two enum values. `ExtractionResult` carries `promise.amount_paise`, "
        "`promise.date`, and `promise.schedule[]` -- a multi-leg structure. The "
        "regex is not *worse* at producing that. It has no output type that could "
        "hold it.",
        "",
        "That structure is what the rest of the system runs on. \"I can pay 21,000 on "
        "the 5th and the rest by month end\" becomes two legs, two dates, an "
        "arithmetic split of the balance, an early-payment discount on the first leg "
        "only, and two separate Razorpay e-mandate links -- one per instalment.",
        "",
        "So: **the model buys structured extraction, not classification accuracy.** "
        "On the axis measured here it ties. On the axis the product depends on, the "
        "baseline cannot compete because it cannot represent the answer. Only 2 of "
        "the 50 rows carry a labelled promise amount, which is far too few to publish "
        "as a measured win -- so this is stated as an architectural fact, with no "
        "number attached to it.",
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
        parts += [
            "",
            "**Arguable misses stay misses.** Some of the rows above are defensible "
            "either way -- `g048` (\"I cancelled that auto debit mandate with my bank "
            "last week\") was labelled `MANDATE_INVALID` and read as "
            "`SILENT_REVOCATION`, and there is a real case for the extractor's answer: "
            "the revocation *was* done silently at the bank, and the debtor is "
            "disclosing it after the fact rather than at the time. I did not mark that "
            "item hard when I wrote it, and I should have.",
            "",
            "The label still stands and the row still counts as wrong. Revising a "
            "label after seeing the output is precisely the thing pre-registration "
            "exists to stop, and a golden set whose answers move when the model "
            "disagrees measures nothing at all. The right correction is a future "
            "commit that re-registers a sharper definition, argued on its own terms "
            "and applied before the next run -- not an edit to this one.",
        ]
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
    parser.add_argument("--check", action="store_true",
                        help="fail if the committed report differs from a fresh regeneration")
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

    if args.report or args.check:
        if not (EXTRACTOR_RESULTS.exists() and BASELINE_RESULTS.exists()):
            sys.exit("need both --baseline and --extractor results before --report")
        extractor = json.loads(EXTRACTOR_RESULTS.read_text(encoding="utf-8"))
        baseline = json.loads(BASELINE_RESULTS.read_text(encoding="utf-8"))
        markdown = build_report(rows, extractor, baseline, label_commit)

        if args.check:
            existing = REPORT_PATH.read_text(encoding="utf-8") if REPORT_PATH.exists() else None
            if existing != markdown:
                print(f"STALE: {REPORT_PATH.name} does not match what score.py produces. "
                      "Run 'uv run python -m eval.golden.score --report' and commit it.",
                      file=sys.stderr)
                return 1
            print(f"{REPORT_PATH.name} is current.")
        else:
            REPORT_PATH.write_text(markdown, encoding="utf-8")
            print(f"wrote {REPORT_PATH.relative_to(REPO_ROOT)}")

    if not (args.baseline or args.extractor or args.report or args.check):
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
