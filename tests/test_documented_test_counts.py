"""The docs claim a test count. This asserts the claim is true.

An external audit found three different numbers for one fact -- README said
789, docs/LIMITATIONS.md said 771, the suite actually collected 856. The
damage isn't the stale number itself; it's that in a project whose entire
register is "every number here was checked", a reader who catches one
unchecked number starts discounting the rest.

Hand-fixing them would have left the same hole open, so this closes it: the
documented counts are checked against a real collection, the same way every
other number in this repo is checked against a real run.

Both docs state the same fact in two halves -- the tests that run with no
credentials, and the live-rail ones skipped without them -- so both are
checked against the one number a subprocess can establish cheaply and
unambiguously: what the suite actually collects. Their two halves must sum
to it.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
LIMITATIONS = REPO_ROOT / "docs" / "LIMITATIONS.md"


@pytest.fixture(scope="module")
def collected_total() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:randomly", str(REPO_ROOT / "tests")],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    match = re.search(r"(\d+) tests? collected", result.stdout)
    if match is None:  # pragma: no cover -- only when collection itself breaks
        pytest.fail(f"could not parse collection output:\n{result.stdout[-2000:]}")
    return int(match.group(1))


def test_readme_test_count_matches_a_real_collection(collected_total):
    """The README now states the *collected* total directly, rather than
    two numbers a reader has to add up.

    That phrasing exists because "984 tests" beside a run that reports 995
    collected makes a reader stop and wonder which is wrong -- and the
    answer, that 11 need live Razorpay credentials, was a click away
    instead of on the line. Asserting the collected figure directly also
    makes this check stricter: the old form passed as long as the two
    numbers summed, so a wrong split could hide inside a right total.
    """
    text = README.read_text(encoding="utf-8")
    claimed = re.search(
        r"uv run pytest\s+#\s*([\d,]+) collected:\s*([\d,]+) run without credentials,\s*(\d+) skipped",
        text,
    )
    assert claimed is not None, "README no longer states its test count in the expected form"
    collected = int(claimed.group(1).replace(",", ""))
    run = int(claimed.group(2).replace(",", ""))
    skipped = int(claimed.group(3))

    assert collected == collected_total, (
        f"README claims {collected} collected; a real collection finds "
        f"{collected_total}. Update the README."
    )
    assert run + skipped == collected_total, (
        f"README's split ({run} run + {skipped} skipped = {run + skipped}) does not "
        f"add up to the {collected_total} it collects."
    )


def test_limitations_test_count_matches_a_real_collection(collected_total):
    text = LIMITATIONS.read_text(encoding="utf-8")
    claimed = re.search(r"\*\*([\d,]+) collected / ([\d,]+) passing / (\d+) skipped", text)
    assert claimed is not None, "docs/LIMITATIONS.md no longer states its test count in the expected form"
    collected = int(claimed.group(1).replace(",", ""))
    passing = int(claimed.group(2).replace(",", ""))
    skipped = int(claimed.group(3))

    assert collected == collected_total, (
        f"docs/LIMITATIONS.md claims {collected} collected; a real collection finds "
        f"{collected_total}. Update it."
    )
    assert passing + skipped == collected_total, (
        f"docs/LIMITATIONS.md's split ({passing} passing + {skipped} skipped) does not "
        f"add up to the {collected_total} it collects."
    )
