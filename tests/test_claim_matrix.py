"""The claim matrix has to survive its own standard.

`docs/CLAIM_MATRIX.md` maps every quantitative claim to the test that
guards it. A document making that promise, which itself silently rots when
a test is renamed, would be worse than not writing it -- it would be a
credibility claim backed by nothing, which is the exact failure mode the
matrix exists to rule out.

So: every test node id cited in the matrix must actually be collectable,
and every file path must exist. If someone renames a test, this fails and
names the stale row.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MATRIX = REPO / "docs" / "CLAIM_MATRIX.md"

# `tests/path.py::Class::test_name` or `tests/path.py::test_name`, as cited
# inside backticks in the table's "Guarded by" column.
NODE_RE = re.compile(r"`(tests/[\w/]+\.py(?:::[\w]+)+)`")
FILE_RE = re.compile(r"`(tests/[\w/]+\.py)`")


@pytest.fixture(scope="module")
def matrix_text() -> str:
    assert MATRIX.exists(), "docs/CLAIM_MATRIX.md is referenced by the README and must exist"
    return MATRIX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def collected_nodes() -> set[str]:
    """Every node id pytest can actually collect, normalised to forward
    slashes so this passes on Windows and Linux alike."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:randomly", str(REPO / "tests")],
        cwd=REPO, capture_output=True, text=True,
    )
    nodes = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if "::" not in line:
            continue
        # Drop a parametrisation suffix: the matrix cites the test, not a case.
        nodes.add(line.split("[")[0].replace("\\", "/"))
    assert nodes, f"collected nothing -- pytest said:\n{result.stdout[-2000:]}"
    return nodes


class TestEveryCitedGuardExists:
    def test_the_matrix_cites_some_guards_at_all(self, matrix_text):
        """Guards against this whole file passing vacuously if the table's
        formatting changes and the regex stops matching anything."""
        assert len(NODE_RE.findall(matrix_text)) >= 8

    def test_every_cited_test_node_is_collectable(self, matrix_text, collected_nodes):
        cited = sorted(set(NODE_RE.findall(matrix_text)))
        missing = [node for node in cited if node not in collected_nodes]
        assert not missing, (
            "docs/CLAIM_MATRIX.md cites test nodes that no longer exist:\n  "
            + "\n  ".join(missing)
            + "\n\nA claim matrix pointing at a renamed test is worse than no "
              "claim matrix. Update the row or restore the test."
        )

    def test_every_cited_test_file_exists(self, matrix_text):
        cited = sorted(set(FILE_RE.findall(matrix_text)))
        missing = [path for path in cited if not (REPO / path).exists()]
        assert not missing, f"docs/CLAIM_MATRIX.md cites missing test files: {missing}"


class TestItStaysHonestAboutGaps:
    def test_it_names_the_claims_with_no_automated_guard(self, matrix_text):
        """The section that makes the rest believable. A matrix that implied
        everything was gated would be overclaiming in a document whose whole
        purpose is not to."""
        assert "Claims with no automated guard" in matrix_text

    def test_the_live_rail_figures_are_marked_as_ungated(self, matrix_text):
        """CI cannot re-derive a number that costs real API calls, and the
        matrix must say so rather than implying a guard exists."""
        section = matrix_text.split("Claims with no automated guard", 1)[1]
        assert "215,867" in section

    def test_the_readme_points_at_it(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        assert "CLAIM_MATRIX.md" in readme, (
            "a claim matrix nobody is directed to is a claim matrix nobody reads")
