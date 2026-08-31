"""Tests for agent.auditor.extraction_log -- the local, opt-in record of
past Path B extractions that agent.auditor.extractor_drift samples from."""

from __future__ import annotations

import pytest

from agent.auditor.extraction_log import ExtractionLog
from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family


@pytest.fixture
def log(tmp_path):
    with ExtractionLog(tmp_path / "extraction_log.db") as el:
        yield el


def _result(**overrides) -> ExtractionResult:
    defaults = dict(family=Family.C, **{"class": DiagnosisClass.PROMISE_STATED}, confidence=0.8)
    defaults.update(overrides)
    return ExtractionResult(**defaults)


def test_record_and_read_back(log):
    log.record(reply_text="will pay Friday", result=_result(), model="claude-sonnet-5", purpose="path_b_extraction")
    entries = log.all_entries()
    assert len(entries) == 1
    assert entries[0].reply_text == "will pay Friday"
    assert entries[0].family == Family.C
    assert entries[0].class_ == DiagnosisClass.PROMISE_STATED
    assert entries[0].confidence == 0.8
    assert entries[0].model == "claude-sonnet-5"


def test_count_reflects_number_of_records(log):
    assert log.count() == 0
    log.record(reply_text="a", result=_result(), model="m", purpose="p")
    log.record(reply_text="b", result=_result(), model="m", purpose="p")
    assert log.count() == 2


def test_entries_returned_in_insertion_order(log):
    log.record(reply_text="first", result=_result(), model="m", purpose="p")
    log.record(reply_text="second", result=_result(), model="m", purpose="p")
    entries = log.all_entries()
    assert [e.reply_text for e in entries] == ["first", "second"]


def test_record_returns_the_new_row_id(log):
    first_id = log.record(reply_text="a", result=_result(), model="m", purpose="p")
    second_id = log.record(reply_text="b", result=_result(), model="m", purpose="p")
    assert second_id > first_id
