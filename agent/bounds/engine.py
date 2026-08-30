"""BOUNDS stage: check_bounds(), the single gate every action passes through (Law 3).
DEVDOC_v6 §13.

check_bounds() evaluates every rule — it does not short-circuit on the first
refusal — so the full verdict list is available for the ledger's
`bounds_checks` (§11.1) and the refusal log (§13.3). A refusal is routine,
expected behaviour here, not an exceptional condition, so this returns a
result object rather than raising; `BoundsRefusal` is provided for a caller
that specifically wants refuse-or-raise semantics (e.g. a strict test).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time as time_cls
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Callable, Literal

import yaml

from agent.bounds.context import BoundsContext
from agent.bounds.expr import BoundsExpr

_DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "rules.yaml"

RuleKind = Literal["regulatory", "stopping"]
Verdict = Literal["PASS", "REFUSE"]


def _implies(a: object, b: object) -> bool:
    return (not a) or bool(b)


def _superset(fields: object, required: object) -> bool:
    return set(required).issubset(set(fields))  # type: ignore[arg-type]


DEFAULT_FUNCTIONS: dict[str, Callable] = {
    "implies": _implies,
    "superset": _superset,
    "len": len,
    "set": set,
    "time": time_cls,
    "timedelta": timedelta,
}


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    kind: RuleKind
    expr: BoundsExpr
    human: str
    test: str
    source: str | None = None
    clause_ref: str | None = None


@dataclass(frozen=True, slots=True)
class BoundsVerdict:
    rule_id: str
    verdict: Verdict
    reason: str
    kind: RuleKind

    def to_dict(self) -> dict[str, str]:
        return {"rule_id": self.rule_id, "verdict": self.verdict, "reason": self.reason, "kind": self.kind}


@dataclass(frozen=True, slots=True)
class BoundsResult:
    verdicts: tuple[BoundsVerdict, ...]

    @property
    def passed(self) -> bool:
        return all(v.verdict == "PASS" for v in self.verdicts)

    @property
    def refusals(self) -> tuple[BoundsVerdict, ...]:
        return tuple(v for v in self.verdicts if v.verdict == "REFUSE")


class BoundsRefusal(Exception):
    """Raise-on-refuse variant for a caller that wants refusal to be exceptional."""

    def __init__(self, result: BoundsResult):
        self.result = result
        super().__init__(
            "check_bounds refused: " + "; ".join(f"{v.rule_id} ({v.reason})" for v in result.refusals)
        )


def load_rules(path: Path | str = _DEFAULT_RULES_PATH) -> list[Rule]:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    rules: list[Rule] = []
    for kind in ("regulatory", "stopping"):
        for row in raw.get(kind, []):
            rules.append(
                Rule(
                    id=row["id"],
                    kind=kind,  # type: ignore[arg-type]
                    expr=BoundsExpr(row["machine"]),
                    human=row["human"],
                    test=row["test"],
                    source=row.get("source"),
                    clause_ref=row.get("clause_ref"),
                )
            )
    return rules


@lru_cache(maxsize=1)
def default_rules() -> tuple[Rule, ...]:
    return tuple(load_rules())


def check_bounds(
    context: BoundsContext,
    *,
    rules: tuple[Rule, ...] | list[Rule] | None = None,
    functions: dict[str, Callable] | None = None,
) -> BoundsResult:
    active_rules = rules if rules is not None else default_rules()
    funcs = functions or DEFAULT_FUNCTIONS
    namespace = context.to_namespace()

    verdicts = []
    for rule in active_rules:
        passed = bool(rule.expr.evaluate(namespace, funcs))
        verdicts.append(
            BoundsVerdict(
                rule_id=rule.id,
                verdict="PASS" if passed else "REFUSE",
                reason="ok" if passed else rule.human,
                kind=rule.kind,
            )
        )
    return BoundsResult(verdicts=tuple(verdicts))
