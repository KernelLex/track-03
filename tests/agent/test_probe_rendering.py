"""The one pure-logic part of tools/probe_rails.py — everything else needs a live
network call and real test keys, so it can't be exercised in this test suite."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "probe_rails", Path(__file__).resolve().parents[2] / "tools" / "probe_rails.py"
)
probe_rails = importlib.util.module_from_spec(_SPEC)
sys.modules["probe_rails"] = probe_rails
_SPEC.loader.exec_module(probe_rails)  # type: ignore[union-attr]


def test_render_markdown_produces_a_row_per_probe():
    results = [
        probe_rails.ProbeResult("orders", True, 200, None, "reachable"),
        probe_rails.ProbeResult("emandate", False, None, "ENABLEMENT_REQUIRED", "not enabled on this account"),
    ]
    markdown = probe_rails.render_markdown(results)
    assert "| `orders` | cleared |" in markdown
    assert "| `emandate` | blocked |" in markdown
    assert "ENABLEMENT_REQUIRED" in markdown


def test_render_markdown_strips_embedded_newlines_from_the_description():
    results = [probe_rails.ProbeResult("orders", False, 400, "BAD_REQUEST", "first line\nsecond line")]
    markdown = probe_rails.render_markdown(results)
    row = next(line for line in markdown.splitlines() if line.startswith("| `orders` |"))
    assert "second line" in row  # survived, just joined onto the same row
    assert markdown.count("\n") == len(markdown.splitlines())  # no embedded newline split a row in two


def test_render_markdown_truncates_overlong_descriptions():
    results = [probe_rails.ProbeResult("orders", False, 400, "BAD_REQUEST", "x" * 500)]
    markdown = probe_rails.render_markdown(results)
    row = next(line for line in markdown.splitlines() if line.startswith("| `orders` |"))
    assert len(row) < 260  # description was capped, not emitted at full length
