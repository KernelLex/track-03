"""The golden set is data, and data with a typo in it silently corrupts the
metric it feeds. These check the set itself, the baseline, and the scoring
arithmetic -- none of them call a model.
"""

from __future__ import annotations

import json

import pytest

from agent.diagnose.extract import FAMILY_CLASSES, DiagnosisClass, Family
from eval.golden.baseline import FALLBACK, classify
from eval.golden.score import load_golden, score

GOLDEN = load_golden()


class TestTheSetItself:
    def test_it_has_the_documented_size(self):
        assert len(GOLDEN) == 50

    def test_ids_are_unique(self):
        ids = [row["id"] for row in GOLDEN]
        assert len(set(ids)) == len(ids)

    @pytest.mark.parametrize("row", GOLDEN, ids=[r["id"] for r in GOLDEN])
    def test_every_label_is_a_real_family_class_pair(self, row):
        """A class assigned to the wrong family would be unscoreable: the
        extractor structurally cannot return that pair, so the row would
        count as a permanent miss for a reason that is my error, not the
        model's."""
        family = Family(row["family"])
        class_ = DiagnosisClass(row["class"])
        assert class_ in FAMILY_CLASSES[family], (
            f"{row['id']}: {class_.value} does not belong to family {family.value}")

    @pytest.mark.parametrize("row", GOLDEN, ids=[r["id"] for r in GOLDEN])
    def test_every_row_carries_a_rationale(self, row):
        """A label with no stated reason cannot be argued with, which makes
        the whole set unfalsifiable."""
        assert row.get("note", "").strip(), f"{row['id']} has no note"
        assert row["text"].strip()

    def test_silent_is_excluded(self):
        """SILENT is the absence of a reply. A row labelled SILENT would be
        a category error -- there would be no text to classify."""
        assert not any(row["class"] == DiagnosisClass.SILENT.value for row in GOLDEN)

    def test_all_four_families_are_represented(self):
        families = {row["family"] for row in GOLDEN}
        assert families == {f.value for f in Family}

    def test_the_hard_items_are_marked_and_explained(self):
        hard = [row for row in GOLDEN if row.get("hard")]
        assert 3 <= len(hard) <= 15, "a set with no hard items, or mostly hard items, measures the wrong thing"
        for row in hard:
            assert "ambiguous" in row["note"].lower() or "hedged" in row["note"].lower(), (
                f"{row['id']} is marked hard but its note does not say against what")

    def test_the_one_live_reply_is_marked_as_such(self):
        live = [row for row in GOLDEN if row.get("source") == "live"]
        assert len(live) == 1, "the report claims exactly one harvested reply"
        assert live[0]["id"] == "g006"

    def test_it_is_valid_jsonl(self):
        from eval.golden.score import GOLDEN_PATH
        for i, line in enumerate(GOLDEN_PATH.read_text(encoding="utf-8").splitlines(), start=1):
            if line.strip():
                json.loads(line)  # raises with the line number in context if not


class TestTheBaseline:
    @pytest.mark.parametrize("row", GOLDEN, ids=[r["id"] for r in GOLDEN])
    def test_it_always_returns_a_valid_pair(self, row):
        family, class_ = classify(row["text"])
        assert class_ in FAMILY_CLASSES[family]

    def test_it_is_deterministic(self):
        first = [classify(row["text"]) for row in GOLDEN]
        second = [classify(row["text"]) for row in GOLDEN]
        assert first == second

    def test_it_falls_back_rather_than_raising_on_unmatched_text(self):
        assert classify("qwertyuiop zxcvbnm") == FALLBACK

    def test_it_is_a_real_attempt_not_a_strawman(self):
        """If the baseline were trivially bad, beating it would prove
        nothing. It has to get a substantial fraction right for the
        comparison to be worth publishing."""
        predictions = [{"id": r["id"], "predicted_family": classify(r["text"])[0].value,
                        "predicted_class": classify(r["text"])[1].value} for r in GOLDEN]
        result = score(GOLDEN, predictions)
        assert result["family_accuracy"].point >= 0.50, (
            "a baseline this weak would make the extractor's win meaningless")

    def test_it_does_not_solve_the_task(self):
        """And if it were perfect, the model would be unnecessary -- which
        would also be a finding, just a different one."""
        predictions = [{"id": r["id"], "predicted_family": classify(r["text"])[0].value,
                        "predicted_class": classify(r["text"])[1].value} for r in GOLDEN]
        assert score(GOLDEN, predictions)["class_accuracy"].point < 1.0


class TestScoringArithmetic:
    """Scoring computed against a hand-built case, so a bug in the counter
    cannot hide inside a plausible-looking accuracy number."""

    ROWS = [
        {"id": "a", "text": "x", "family": "B", "class": "PO_MISMATCH", "hard": False, "note": "n"},
        {"id": "b", "text": "x", "family": "B", "class": "GST_DEFECT", "hard": False, "note": "n"},
        {"id": "c", "text": "x", "family": "D", "class": "AMOUNT", "hard": True, "note": "n"},
        {"id": "d", "text": "x", "family": "C", "class": "STALLING", "hard": False, "note": "n"},
    ]

    def test_counts_family_and_class_separately(self):
        predictions = [
            {"id": "a", "predicted_family": "B", "predicted_class": "PO_MISMATCH"},    # both right
            {"id": "b", "predicted_family": "B", "predicted_class": "PO_MISMATCH"},    # family right, class wrong
            {"id": "c", "predicted_family": "C", "predicted_class": "STALLING"},       # both wrong
            {"id": "d", "predicted_family": "C", "predicted_class": "STALLING"},       # both right
        ]
        result = score(self.ROWS, predictions)
        assert result["class_correct"] == 2
        assert result["family_correct"] == 3
        assert result["n"] == 4

    def test_hard_and_easy_partition_the_set(self):
        predictions = [{"id": r["id"], "predicted_family": r["family"],
                        "predicted_class": r["class"]} for r in self.ROWS]
        result = score(self.ROWS, predictions)
        assert result["easy"]["n"] + result["hard"]["n"] == result["n"]
        assert result["hard"]["n"] == 1

    def test_a_missing_prediction_counts_as_wrong_not_as_absent(self):
        """An extractor that raised on a row must be scored as having got
        that row wrong. Dropping it would flatter the accuracy."""
        result = score(self.ROWS, [])
        assert result["class_correct"] == 0
        assert result["n"] == 4
        assert len(result["errors"]) == 4

    def test_errors_carry_enough_to_argue_with(self):
        predictions = [{"id": "a", "predicted_family": "C", "predicted_class": "STALLING"}]
        result = score(self.ROWS[:1], predictions)
        error = result["errors"][0]
        assert error["want_class"] == "PO_MISMATCH"
        assert error["got_class"] == "STALLING"
        assert error["note"]
