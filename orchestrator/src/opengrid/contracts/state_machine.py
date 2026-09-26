"""Pure obligation state machine (02a S2.1). No I/O, no trace, no DB -- just "is this transition
legal, and does it carry a reason code the transition requires". `lifecycle.py` wraps this with the
DB write and the trace append; `opengrid.selector`/`opengrid.ledger`/`opengrid.settle` never write
`obligation.state` directly, they call `lifecycle.transition_obligation`.

The K13 commitment lock is encoded structurally here: once an obligation reaches `COMMITTED`, the
only reachable states are `DELIVERING` (no exception needed) and, from `DELIVERING`, `FULFILLED` or
`SHORTFALL` -- and `SHORTFALL` is only reachable with one of the four allowed override/infeasible
reason codes (or the disabled `R-AS-RELEASE`, checked separately by `opengrid.core.limits
.check_commitment_lock` when a `commitment` row is superseded). There is no path out of
`COMMITTED`/`DELIVERING` that skips a reason code. A `SHORTFALL` obligation served in full again goes
back to `DELIVERING` with `R-SHORTFALL-RESTORED` (the only way out of `SHORTFALL` besides `SETTLED`).
"""

from __future__ import annotations

from opengrid.contracts.errors import IllegalTransitionError
from opengrid.core.models.engine import ObligationState

#: Reason codes 02a S2.1 allows for a locked (`COMMITTED`/`DELIVERING`) obligation to become
#: `SHORTFALL`. `R-AS-RELEASE` is intentionally excluded here: it is disabled by the
#: `as_release_enabled` feature flag for all of MVP-S (02a S2.1's "fifth path"), and re-enabling it
#: is an `opengrid.core.limits.check_commitment_lock` config change, not a state-machine change.
SHORTFALL_REASON_CODES: frozenset[str] = frozenset(
    {
        "R-COMMIT-LOCK-OVERRIDE-L0",
        "R-COMMIT-LOCK-OVERRIDE-L1",
        "R-COMMIT-LOCK-OVERRIDE-L2",
        "R-COMMIT-LOCK-INFEASIBLE",
        "R-SHORTFALL-THRESHOLD",  # ordinary window-end underperformance, no exception involved
    }
)

LOCKED_STATES: frozenset[ObligationState] = frozenset({"COMMITTED", "DELIVERING"})
TERMINAL_STATES: frozenset[ObligationState] = frozenset({"REJECTED", "EXPIRED", "SETTLED"})

# {from_state: {to_state: required_reason_codes | None}}. `None` means any reason code (including
# `None`) is accepted for that edge -- it does not gate the K13 lock.
_TRANSITIONS: dict[ObligationState, dict[ObligationState, frozenset[str] | None]] = {
    "OFFERED": {
        "SELECTED": frozenset({"R-GATE-SELECT"}),
        "REJECTED": frozenset({"R-ADMIT-REJECT"}),
        "EXPIRED": frozenset({"R-EXPIRED-UNSELECTED"}),
    },
    "SELECTED": {
        "COMMITTED": frozenset({"R-COMMIT-LOCK-ENTER"}),
        "REJECTED": frozenset({"R-COMMIT-LOCK-INFEASIBLE"}),
    },
    "COMMITTED": {
        "DELIVERING": None,
    },
    "DELIVERING": {
        "DELIVERING": frozenset({"R-RENOM-GATE"}),
        "FULFILLED": frozenset({"R-FULFILLED"}),
        "SHORTFALL": SHORTFALL_REASON_CODES,
    },
    "FULFILLED": {
        "SETTLED": None,
    },
    "SHORTFALL": {
        # Owner decision 2026-09-26 (K13 best effort): once the constraint clears and the obligation is
        # served its full committed kW again, it returns to DELIVERING (and is locked again).
        "DELIVERING": frozenset({"R-SHORTFALL-RESTORED"}),
        "SETTLED": None,
    },
    "REJECTED": {},
    "EXPIRED": {},
    "SETTLED": {},
}


def allowed_next_states(current: ObligationState) -> frozenset[ObligationState]:
    """Every state `current` may legally transition to (ignoring reason-code gating)."""
    return frozenset(_TRANSITIONS.get(current, {}).keys())


def is_locked(state: ObligationState) -> bool:
    """K13: true while the commitment lock applies (`COMMITTED` or `DELIVERING`)."""
    return state in LOCKED_STATES


def is_terminal(state: ObligationState) -> bool:
    return state in TERMINAL_STATES


def validate_transition(
    current: ObligationState,
    target: ObligationState,
    reason_code: str | None,
) -> None:
    """Raise `IllegalTransitionError` unless `current -> target` is a legal 02a S2.1 edge carrying
    a reason code that edge requires. Never mutates anything -- callers persist only after this
    passes. This is the single place the obligation FSM is defined; nothing else re-implements it.
    """
    edges = _TRANSITIONS.get(current)
    if not edges or target not in edges:
        raise IllegalTransitionError(from_state=current, to_state=target, reason_code=reason_code)
    required = edges[target]
    if required is not None and reason_code not in required:
        raise IllegalTransitionError(from_state=current, to_state=target, reason_code=reason_code)
