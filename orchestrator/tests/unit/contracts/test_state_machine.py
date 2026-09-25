"""Property tests for the obligation FSM (02a S2.1, K13). No DB/trace involved -- this exercises
`state_machine.py` directly, which is pure and total over every `(state, state, reason_code)` triple.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from opengrid.contracts.errors import IllegalTransitionError
from opengrid.contracts.state_machine import (
    _TRANSITIONS,
    SHORTFALL_REASON_CODES,
    is_locked,
    is_terminal,
    validate_transition,
)
from opengrid.core.models.engine import ObligationState

ALL_STATES: tuple[ObligationState, ...] = (
    "OFFERED",
    "SELECTED",
    "COMMITTED",
    "DELIVERING",
    "FULFILLED",
    "SHORTFALL",
    "SETTLED",
    "REJECTED",
    "EXPIRED",
)

ALL_REASON_CODES = (
    None,
    "R-GATE-SELECT",
    "R-ADMIT-REJECT",
    "R-EXPIRED-UNSELECTED",
    "R-COMMIT-LOCK-ENTER",
    "R-COMMIT-LOCK-INFEASIBLE",
    "R-RENOM-GATE",
    "R-FULFILLED",
    "R-COMMIT-LOCK-OVERRIDE-L0",
    "R-COMMIT-LOCK-OVERRIDE-L1",
    "R-COMMIT-LOCK-OVERRIDE-L2",
    "R-SHORTFALL-THRESHOLD",
    "R-AS-RELEASE",
    "R-SUBSTITUTION",
    "bogus-code",
)

states_st = st.sampled_from(ALL_STATES)
reasons_st = st.sampled_from(ALL_REASON_CODES)


def _is_legal(current: ObligationState, target: ObligationState, reason_code: str | None) -> bool:
    edges = _TRANSITIONS.get(current, {})
    if target not in edges:
        return False
    required = edges[target]
    return required is None or reason_code in required


@given(current=states_st, target=states_st, reason_code=reasons_st)
def test_validate_transition_matches_reference_table(
    current: ObligationState, target: ObligationState, reason_code: str | None
) -> None:
    """Every accept/reject decision matches a hand-checked re-derivation of the transition table --
    the property-test form of "random event sequences never reach an illegal state"."""
    if _is_legal(current, target, reason_code):
        validate_transition(current, target, reason_code)
    else:
        try:
            validate_transition(current, target, reason_code)
        except IllegalTransitionError:
            pass
        else:
            raise AssertionError(
                f"expected IllegalTransitionError for {current} -> {target} ({reason_code!r})"
            )


@given(sequence=st.lists(st.tuples(states_st, reasons_st), min_size=1, max_size=25))
def test_random_event_sequences_never_reach_an_illegal_state(
    sequence: list[tuple[ObligationState, str | None]],
) -> None:
    """Drive a fresh obligation through an arbitrary sequence of (target_state, reason_code) events,
    applying only legal ones. The obligation always ends in a real, reachable state, and every
    rejected event leaves the current state unchanged (no partial/illegal state is ever entered)."""
    state: ObligationState = "OFFERED"
    for target, reason_code in sequence:
        if _is_legal(state, target, reason_code):
            validate_transition(state, target, reason_code)
            state = target
        else:
            try:
                validate_transition(state, target, reason_code)
            except IllegalTransitionError:
                pass
            else:
                raise AssertionError("illegal event was silently accepted")
        assert state in ALL_STATES


@given(sequence=st.lists(st.tuples(states_st, reasons_st), min_size=1, max_size=25))
def test_committed_is_never_silently_released(sequence: list[tuple[ObligationState, str | None]]) -> None:
    """Once locked (COMMITTED/DELIVERING), the obligation can only ever leave via FULFILLED or a
    SHORTFALL carrying one of the K13 override/infeasibility reason codes -- never REJECTED/EXPIRED,
    and never SHORTFALL with an arbitrary or missing reason code (K13)."""
    state: ObligationState = "OFFERED"
    for target, reason_code in sequence:
        was_locked = is_locked(state)
        if _is_legal(state, target, reason_code):
            validate_transition(state, target, reason_code)
            if was_locked and target != state:
                assert target in ("DELIVERING", "FULFILLED", "SHORTFALL")
                if target == "SHORTFALL":
                    assert reason_code in SHORTFALL_REASON_CODES
                    assert reason_code != "R-AS-RELEASE"
            state = target


def test_terminal_states_have_no_outgoing_edges() -> None:
    for state in ("REJECTED", "EXPIRED", "SETTLED"):
        assert is_terminal(state)  # type: ignore[arg-type]
        for target in ALL_STATES:
            try:
                validate_transition(state, target, "any-reason")  # type: ignore[arg-type]
            except IllegalTransitionError:
                continue
            raise AssertionError(f"terminal state {state} accepted a transition to {target}")


def test_r_as_release_alone_never_authorizes_a_shortfall_transition() -> None:
    """The disabled fifth path (R-AS-RELEASE, 02a S2.1) must never satisfy the obligation FSM by
    itself -- re-enabling it is a `core.limits.check_commitment_lock` flag change, not an FSM edge."""
    try:
        validate_transition("DELIVERING", "SHORTFALL", "R-AS-RELEASE")
    except IllegalTransitionError:
        pass
    else:
        raise AssertionError("R-AS-RELEASE alone should not authorize DELIVERING -> SHORTFALL")
