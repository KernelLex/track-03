"""Fact provenance model and the LedgerEntry record. DEVDOC_v6 §8, §15."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Literal

RailTag = Literal["razorpay", "simulated"]


class Provenance(str, Enum):
    """Where a fact came from. Only SYSTEM and HUMAN facts may feed a legal computation. §8."""

    SYSTEM = "SYSTEM"
    MODEL = "MODEL"
    HUMAN = "HUMAN"


class ProvenanceViolation(Exception):
    """Raised when a MODEL-provenance fact reaches a legal computation. A crash, not a warning. §8."""


@dataclass(frozen=True, slots=True)
class Fact:
    name: str
    value: Any
    provenance: Provenance
    source_ref: str | None = None
    """Webhook event id, human approval id, or model extraction id this fact traces to."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "provenance": self.provenance.value,
            "source_ref": self.source_ref,
        }


def assert_legal_provenance(facts: list[Fact]) -> None:
    """Guard for every legal_computation(). Raises ProvenanceViolation — never a warning. §8."""
    for f in facts:
        if f.provenance == Provenance.MODEL:
            raise ProvenanceViolation(
                f"fact {f.name!r} has MODEL provenance and cannot feed a legal computation "
                f"(source_ref={f.source_ref!r}) — see DEVDOC_v6 §8"
            )


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One row of the append-only, hash-chained ledger. DEVDOC_v6 §15.

    `seq`, `prev_hash` and `hash` are assigned by `Ledger.append()` — construct
    an entry without them (they default to None) and pass it to `append()`,
    which returns the finalized, chain-linked entry.
    """

    actor: str
    debtor_id: str
    observation_refs: list[str] = field(default_factory=list)
    ts: str | None = None
    """Server-assigned UTC ISO8601, set by Ledger.append() like seq/prev_hash/hash. Never caller-supplied."""
    model_version: str | None = None
    prompt_version: str | None = None
    rulebook_version: str = "unversioned"
    decision: dict[str, Any] | None = None
    bounds_checks: list[dict[str, Any]] = field(default_factory=list)
    action: dict[str, Any] | None = None
    idempotency_key: str | None = None
    facts_used: list[Fact] = field(default_factory=list)
    mandate_ref: str | None = None
    rail_tag: RailTag | None = None
    outcome: dict[str, Any] | None = None
    seq: int | None = None
    prev_hash: str | None = None
    hash: str | None = None

    def payload(self) -> dict[str, Any]:
        """Everything that gets hashed — excludes seq/prev_hash/hash, which wrap this payload."""
        return {
            "actor": self.actor,
            "debtor_id": self.debtor_id,
            "observation_refs": list(self.observation_refs),
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "rulebook_version": self.rulebook_version,
            "decision": self.decision,
            "bounds_checks": list(self.bounds_checks),
            "action": self.action,
            "idempotency_key": self.idempotency_key,
            "facts_used": [f.to_dict() for f in self.facts_used],
            "mandate_ref": self.mandate_ref,
            "rail_tag": self.rail_tag,
            "outcome": self.outcome,
        }

    @staticmethod
    def from_row(*, seq: int, prev_hash: str, hash: str, body: dict[str, Any]) -> "LedgerEntry":
        facts = [
            Fact(
                name=fd["name"],
                value=fd["value"],
                provenance=Provenance(fd["provenance"]),
                source_ref=fd.get("source_ref"),
            )
            for fd in body.get("facts_used", [])
        ]
        return LedgerEntry(
            actor=body["actor"],
            debtor_id=body["debtor_id"],
            ts=body.get("ts"),
            observation_refs=list(body.get("observation_refs", [])),
            model_version=body.get("model_version"),
            prompt_version=body.get("prompt_version"),
            rulebook_version=body.get("rulebook_version", "unversioned"),
            decision=body.get("decision"),
            bounds_checks=list(body.get("bounds_checks", [])),
            action=body.get("action"),
            idempotency_key=body.get("idempotency_key"),
            facts_used=facts,
            mandate_ref=body.get("mandate_ref"),
            rail_tag=body.get("rail_tag"),
            outcome=body.get("outcome"),
            seq=seq,
            prev_hash=prev_hash,
            hash=hash,
        )
