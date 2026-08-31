"""Tests for agent.auditor.extractor_drift -- no real model call. `rerun`
is injected as a plain function throughout, exactly the seam the module
exists to offer so this suite needs no live ANTHROPIC_API_KEY."""

from __future__ import annotations

import random

import pytest

from agent.auditor.extraction_log import ExtractionLog
from agent.auditor.extractor_drift import (
    DEFAULT_AGREEMENT_THRESHOLD,
    check_extractor_drift,
    sample_logged_extractions,
)
from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family
from agent.diagnose.llm_extract import ExtractionFailed


@pytest.fixture
def log(tmp_path):
    with ExtractionLog(tmp_path / "extraction_log.db") as el:
        yield el


def _result(**overrides) -> ExtractionResult:
    defaults = dict(family=Family.C, **{"class": DiagnosisClass.PROMISE_STATED}, confidence=0.8)
    defaults.update(overrides)
    return ExtractionResult(**defaults)


def _log_n(log, n, *, family=Family.C, class_=DiagnosisClass.PROMISE_STATED):
    for i in range(n):
        log.record(reply_text=f"reply {i}", result=_result(family=family, **{"class": class_}), model="claude-sonnet-5", purpose="path_b_extraction")


class TestSampleLoggedExtractions:
    def test_empty_log_yields_no_sample(self, log):
        assert sample_logged_extractions(log) == []

    def test_sample_size_is_at_least_one(self, log):
        _log_n(log, 3)
        sample = sample_logged_extractions(log, sample_rate=0.10, rng=random.Random(1))
        assert len(sample) == 1

    def test_sample_rate_above_population_returns_everything(self, log):
        _log_n(log, 4)
        sample = sample_logged_extractions(log, sample_rate=0.99, rng=random.Random(1))
        assert len(sample) == 4


class TestCheckExtractorDrift:
    def test_no_logged_extractions_yields_full_agreement_no_quarantine(self, log):
        report = check_extractor_drift(log, rerun=lambda text: _result())
        assert report.sampled == []
        assert report.agreement_rate == 1.0
        assert report.quarantine is False

    def test_perfect_agreement_never_quarantines(self, log):
        _log_n(log, 10, family=Family.C, class_=DiagnosisClass.PROMISE_STATED)
        report = check_extractor_drift(
            log, sample_rate=1.0, rerun=lambda text: _result(family=Family.C, **{"class": DiagnosisClass.PROMISE_STATED}),
        )
        assert report.agreement_rate == 1.0
        assert report.quarantine is False
        assert all(r.agrees for r in report.sampled)

    def test_total_disagreement_quarantines(self, log):
        _log_n(log, 10, family=Family.C, class_=DiagnosisClass.PROMISE_STATED)
        report = check_extractor_drift(
            log, sample_rate=1.0, rerun=lambda text: _result(family=Family.D, **{"class": DiagnosisClass.AMOUNT}),
        )
        assert report.agreement_rate == 0.0
        assert report.quarantine is True
        assert all(not r.agrees for r in report.sampled)

    def test_agreement_exactly_at_threshold_does_not_quarantine(self, log):
        """quarantine is strictly below threshold, not <=."""
        _log_n(log, 10, family=Family.C, class_=DiagnosisClass.PROMISE_STATED)
        call_count = {"n": 0}

        def rerun(text):
            call_count["n"] += 1
            # 8/10 agree = 0.80, exactly DEFAULT_AGREEMENT_THRESHOLD.
            if call_count["n"] <= 8:
                return _result(family=Family.C, **{"class": DiagnosisClass.PROMISE_STATED})
            return _result(family=Family.D, **{"class": DiagnosisClass.AMOUNT})

        report = check_extractor_drift(log, sample_rate=1.0, rerun=rerun)
        assert report.agreement_rate == pytest.approx(0.8)
        assert report.agreement_rate == DEFAULT_AGREEMENT_THRESHOLD
        assert report.quarantine is False

    def test_a_re_extraction_failure_counts_as_disagreement_not_skipped(self, log):
        _log_n(log, 1)

        def rerun(text):
            raise ExtractionFailed("model did not return a parseable ExtractionResult")

        report = check_extractor_drift(log, sample_rate=1.0, rerun=rerun)
        assert len(report.sampled) == 1
        assert report.sampled[0].agrees is False
        assert report.sampled[0].rerun_family is None
        assert report.quarantine is True  # 0/1 agreement < 0.80

    def test_family_matching_but_class_mismatched_counts_as_disagreement(self, log):
        _log_n(log, 1, family=Family.A, class_=DiagnosisClass.INSUFFICIENT_FUNDS)
        report = check_extractor_drift(
            log, sample_rate=1.0,
            rerun=lambda text: _result(family=Family.A, **{"class": DiagnosisClass.INSTRUMENT_EXPIRED}),
        )
        assert report.sampled[0].agrees is False

    def test_custom_agreement_threshold_is_respected(self, log):
        _log_n(log, 10, family=Family.C, class_=DiagnosisClass.PROMISE_STATED)
        call_count = {"n": 0}

        def rerun(text):
            call_count["n"] += 1
            if call_count["n"] <= 5:  # 50% agreement
                return _result(family=Family.C, **{"class": DiagnosisClass.PROMISE_STATED})
            return _result(family=Family.D, **{"class": DiagnosisClass.AMOUNT})

        lenient = check_extractor_drift(log, sample_rate=1.0, rerun=rerun, agreement_threshold=0.4)
        assert lenient.quarantine is False

    def test_the_uninjected_rerun_path_wires_a_real_extract_from_reply_call(self, log, monkeypatch):
        """Confirms the default (non-injected) path really calls
        agent.diagnose.llm_extract.extract_from_reply with the second
        opinion model -- without hitting a live API."""
        import agent.auditor.extractor_drift as drift_module

        captured = {}

        def fake_extract_from_reply(reply_text, *, client, model, spend_ledger, purpose):
            captured["reply_text"] = reply_text
            captured["model"] = model
            captured["purpose"] = purpose
            return _result()

        monkeypatch.setattr(drift_module, "extract_from_reply", fake_extract_from_reply)
        _log_n(log, 1)
        check_extractor_drift(log, sample_rate=1.0)

        assert captured["model"] == drift_module.SECOND_OPINION_MODEL
        assert captured["purpose"] == "auditor_extractor_drift_recheck"
