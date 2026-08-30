"""Path A: structured, deterministic diagnosis from a Razorpay error code. No LLM. DEVDOC_v6 §11.2.

Loads data/failure_taxonomy.yaml, which is also the entire failure surface
`SimulatedRail` is permitted to emit (§5.4) — one file, two consumers, so
they cannot silently drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml

Disposition = Literal["RETRYABLE", "TERMINAL"]
Rail = Literal["cards", "upi"]

_DEFAULT_TAXONOMY_PATH = Path(__file__).resolve().parents[2] / "data" / "failure_taxonomy.yaml"


class UnknownFailureCode(Exception):
    """Raised when a rail returns a (code, rail) pair not present in the taxonomy.

    This must never be silently swallowed: an unrecognised code means either
    the taxonomy is stale against a live rail, or SimulatedRail emitted
    something outside its permitted surface (§5.4) — both are bugs to see,
    not failures to hide behind a default.
    """


@dataclass(frozen=True, slots=True)
class FailureClassification:
    code: str
    rail: Rail
    source: Literal["customer", "bank", "gateway"]
    disposition: Disposition
    description: str
    note: str | None = None


class FailureTaxonomy:
    def __init__(self, entries: dict[tuple[str, str], FailureClassification]):
        self._entries = entries

    @classmethod
    def load(cls, path: Path | str = _DEFAULT_TAXONOMY_PATH) -> "FailureTaxonomy":
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        entries: dict[tuple[str, str], FailureClassification] = {}
        for rail, codes in raw["codes"].items():
            for row in codes:
                key = (row["code"], rail)
                entries[key] = FailureClassification(
                    code=row["code"],
                    rail=rail,
                    source=row["source"],
                    disposition=row["disposition"],
                    description=row["description"],
                    note=row.get("note"),
                )
        return cls(entries)

    def classify(self, code: str, rail: str) -> FailureClassification:
        key = (code, rail)
        if key not in self._entries:
            raise UnknownFailureCode(f"no taxonomy entry for code={code!r} rail={rail!r}")
        return self._entries[key]

    def permitted_codes(self, rail: str | None = None) -> frozenset[str]:
        """The failure surface SimulatedRail may emit — all codes, or one rail's."""
        return frozenset(code for code, r in self._entries if rail is None or r == rail)

    def __len__(self) -> int:
        return len(self._entries)


@lru_cache(maxsize=1)
def default_taxonomy() -> FailureTaxonomy:
    return FailureTaxonomy.load()
