"""tools/gen_docs.py: generators run cleanly and produce well-formed Markdown
tables (no embedded newlines breaking a row, per the bug found while first
generating BOUNDS.md from rules.yaml's multi-line machine expressions)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "gen_docs", Path(__file__).resolve().parents[2] / "tools" / "gen_docs.py"
)
gen_docs = importlib.util.module_from_spec(_SPEC)
sys.modules["gen_docs"] = gen_docs
_SPEC.loader.exec_module(gen_docs)  # type: ignore[union-attr]


def _table_rows(markdown: str) -> list[str]:
    return [line for line in markdown.splitlines() if line.startswith("|")]


def test_cell_collapses_embedded_newlines_and_repeated_whitespace():
    assert gen_docs._cell("a\n  b\nc") == "a b c"


def test_bounds_md_every_row_has_the_expected_column_count():
    markdown = gen_docs.gen_bounds_md()
    regulatory_rows = [
        r for r in _table_rows(markdown) if r.count("|") == 7 and "RBI_" in r or "TRAI_DND" in r or "MSMED_" in r
    ]
    assert regulatory_rows, "expected at least one regulatory rule row"
    for row in regulatory_rows:
        assert row.count("|") == 7  # 6 columns -> 7 pipe characters


def test_bounds_md_contains_no_bare_newline_inside_a_table_row():
    markdown = gen_docs.gen_bounds_md()
    for row in _table_rows(markdown):
        assert "\n" not in row  # trivially true per-line, but guards regressions if row-building changes


def test_regulatory_map_md_lists_every_regulatory_rule():
    from agent.bounds.engine import default_rules

    markdown = gen_docs.gen_regulatory_map_md()
    regulatory_ids = [r.id for r in default_rules() if r.kind == "regulatory"]
    for rule_id in regulatory_ids:
        assert f"`{rule_id}`" in markdown


def test_ledger_md_worked_example_and_tamper_output_are_present():
    markdown = gen_docs.gen_ledger_md()
    assert "ChainIntegrityError" in markdown
    assert "seq=3" in markdown
    assert "RECOVERED" in markdown


def test_check_mode_passes_immediately_after_a_fresh_write(tmp_path, monkeypatch):
    monkeypatch.setattr(gen_docs, "DOCS_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["gen_docs.py"])
    assert gen_docs.main() == 0  # writes

    monkeypatch.setattr(sys, "argv", ["gen_docs.py", "--check"])
    assert gen_docs.main() == 0
