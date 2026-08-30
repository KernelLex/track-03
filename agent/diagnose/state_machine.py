"""Debtor state machine — state lives on the debtor, not the invoice. DEVDOC_v6 §11.3.

Pure, order-tolerant, exhaustively tested. Illegal transitions raise; they
never silently no-op. The transition table below is the diagram in §11.3
plus the two amendments this build made while implementing it:

- `DIAGNOSED -> HUMAN_QUEUE`: §8's deemed-acceptance worked example states
  the general principle directly — "Any marker, uncertainty or quarantine
  -> HUMAN_QUEUE" — so an uncertain diagnosis must be able to reach the
  queue without detouring through REPAIRING first.
- `DISPUTED_FROZEN -> HUMAN_QUEUE`: §24.2's CLOCK amendment ("Unsubstantiated
  after N days, the case returns to the human queue flagged for review")
  gives this state one legal exit. §11.3's `[bracketed] = terminal` notation
  predates that amendment for this one state — DISPUTED_FROZEN is not
  fully terminal after it, and `is_terminal()` reflects that from the
  transition table rather than the diagram's bracket notation, so the two
  can't silently drift apart again.
"""

from __future__ import annotations

from enum import Enum


class DebtorState(str, Enum):
    HEALTHY = "HEALTHY"
    AT_RISK = "AT_RISK"
    DIAGNOSED = "DIAGNOSED"
    REPAIRING = "REPAIRING"
    RECOVERED = "RECOVERED"
    HUMAN_QUEUE = "HUMAN_QUEUE"
    ENGAGED = "ENGAGED"
    PROMISED = "PROMISED"
    INSTRUMENTED = "INSTRUMENTED"
    BROKEN_PROMISE = "BROKEN_PROMISE"
    MANDATE_DEFECT = "MANDATE_DEFECT"
    DISPUTED_FROZEN = "DISPUTED_FROZEN"
    STATUTORY_PENDING = "STATUTORY_PENDING"
    EXHAUSTED = "EXHAUSTED"


class IllegalTransition(Exception):
    """Raised for any (from_state, to_state) pair outside the transition table.
    Never a silent no-op (§11.3)."""


_TRANSITIONS: dict[DebtorState, frozenset[DebtorState]] = {
    DebtorState.HEALTHY: frozenset({DebtorState.AT_RISK}),
    DebtorState.AT_RISK: frozenset({DebtorState.DIAGNOSED}),
    DebtorState.DIAGNOSED: frozenset({
        DebtorState.REPAIRING,
        DebtorState.ENGAGED,
        DebtorState.MANDATE_DEFECT,
        DebtorState.DISPUTED_FROZEN,
        DebtorState.STATUTORY_PENDING,
        DebtorState.EXHAUSTED,
        DebtorState.HUMAN_QUEUE,
    }),
    DebtorState.REPAIRING: frozenset({
        DebtorState.RECOVERED,
        DebtorState.HUMAN_QUEUE,
        DebtorState.INSTRUMENTED,  # reached via the MANDATE_DEFECT -> REPAIRING -> INSTRUMENTED path
    }),
    DebtorState.ENGAGED: frozenset({DebtorState.PROMISED}),
    DebtorState.PROMISED: frozenset({DebtorState.INSTRUMENTED, DebtorState.BROKEN_PROMISE}),
    DebtorState.INSTRUMENTED: frozenset({DebtorState.RECOVERED}),
    DebtorState.BROKEN_PROMISE: frozenset({DebtorState.ENGAGED}),
    DebtorState.MANDATE_DEFECT: frozenset({DebtorState.REPAIRING}),
    DebtorState.DISPUTED_FROZEN: frozenset({DebtorState.HUMAN_QUEUE}),
    DebtorState.STATUTORY_PENDING: frozenset({
        DebtorState.ENGAGED,
        DebtorState.EXHAUSTED,
        DebtorState.DISPUTED_FROZEN,
    }),
    DebtorState.HUMAN_QUEUE: frozenset({
        DebtorState.RECOVERED,
        DebtorState.ENGAGED,
        DebtorState.REPAIRING,
        DebtorState.DISPUTED_FROZEN,
        DebtorState.STATUTORY_PENDING,
        DebtorState.EXHAUSTED,
    }),
    DebtorState.RECOVERED: frozenset(),
    DebtorState.EXHAUSTED: frozenset(),
}

assert set(_TRANSITIONS) == set(DebtorState), "every DebtorState must have a transition-table entry, even if empty"


def is_terminal(state: DebtorState) -> bool:
    """Derived from the table, not a separately maintained set — so a state whose
    last transition gets removed can't silently stay marked non-terminal, or
    vice versa."""
    return len(_TRANSITIONS[state]) == 0


def legal_next_states(state: DebtorState) -> frozenset[DebtorState]:
    return _TRANSITIONS[state]


def transition(current: DebtorState, target: DebtorState) -> DebtorState:
    """The only sanctioned way to move a debtor between states."""
    if target not in _TRANSITIONS[current]:
        raise IllegalTransition(f"{current.value} -> {target.value} is not a legal transition")
    return target
