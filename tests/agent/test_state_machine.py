"""Exhaustive tests for the debtor state machine, per DEVDOC_v6 §11.3's own standard:
"Pure, order-tolerant, exhaustively tested. Illegal transitions raise; they never
silently no-op." This tests every state x state pair, not a sample of them."""

from __future__ import annotations

import itertools

import pytest

from agent.diagnose.state_machine import DebtorState, IllegalTransition, is_terminal, legal_next_states, transition

ALL_STATES = list(DebtorState)


@pytest.mark.parametrize(
    "current,target",
    [(c, t) for c in ALL_STATES for t in legal_next_states(c)],
    ids=lambda s: s.value if isinstance(s, DebtorState) else str(s),
)
def test_every_legal_transition_succeeds(current: DebtorState, target: DebtorState):
    assert transition(current, target) == target


@pytest.mark.parametrize(
    "current,target",
    [(c, t) for c, t in itertools.product(ALL_STATES, ALL_STATES) if t not in legal_next_states(c)],
    ids=lambda s: s.value if isinstance(s, DebtorState) else str(s),
)
def test_every_other_pair_in_the_full_cartesian_product_is_illegal(current: DebtorState, target: DebtorState):
    with pytest.raises(IllegalTransition):
        transition(current, target)


def test_recovered_and_exhausted_are_the_only_fully_terminal_states():
    terminal = {s for s in ALL_STATES if is_terminal(s)}
    assert terminal == {DebtorState.RECOVERED, DebtorState.EXHAUSTED}


def test_disputed_frozen_is_not_fully_terminal_after_the_24_2_clock_amendment():
    """§24.2's CLOCK amendment gives DISPUTED_FROZEN exactly one exit: to
    HUMAN_QUEUE, once an unsubstantiated dispute's window elapses. It must
    never auto-resolve to anything else — a human decides, always (§24.2)."""
    assert not is_terminal(DebtorState.DISPUTED_FROZEN)
    assert legal_next_states(DebtorState.DISPUTED_FROZEN) == frozenset({DebtorState.HUMAN_QUEUE})


def test_human_queue_can_reach_every_state_a_human_might_route_to_and_no_others():
    expected = {
        DebtorState.RECOVERED, DebtorState.ENGAGED, DebtorState.REPAIRING,
        DebtorState.DISPUTED_FROZEN, DebtorState.STATUTORY_PENDING, DebtorState.EXHAUSTED,
    }
    assert legal_next_states(DebtorState.HUMAN_QUEUE) == frozenset(expected)


def test_every_state_has_a_table_entry_even_if_empty():
    for state in ALL_STATES:
        legal_next_states(state)  # must not raise KeyError for any enum member


def test_diagnosed_can_escalate_directly_to_human_queue_on_uncertainty():
    """§8: 'Any marker, uncertainty or quarantine -> HUMAN_QUEUE' — this must be
    reachable directly from DIAGNOSED, not only after detouring through REPAIRING."""
    assert DebtorState.HUMAN_QUEUE in legal_next_states(DebtorState.DIAGNOSED)
