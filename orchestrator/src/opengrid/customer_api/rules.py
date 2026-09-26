"""What a customer may do to its own obligation, under the commitment lock (K13, 00-invariants.md; 02a
S2.1). Pure functions: no I/O, so the rules are tested exhaustively over every obligation state.

The specs say nothing about customer-initiated cancellation, so the conservative rule is:

- **Cancel before commitment.** Only an `OFFERED` obligation (admitted, not yet selected at a gate) is
  cancelled directly. It leaves selection through the state machine's own `OFFERED -> REJECTED` edge;
  nothing has been reserved for it yet, so no other obligation is affected.
- **Cancel once selected or committed** (`SELECTED`, `COMMITTED`, `DELIVERING`) is never applied by the
  customer. It becomes an operator-reviewed request that carries the contract's penalty terms
  (`penalty_alpha/beta/theta`, 02a S1.2) as they stood when it was made. The obligation keeps its
  commitment and keeps being delivered (K13: an allocation is never reduced before fulfilment or the
  next agreed re-nomination point, except for L0/L1/L2 or physical infeasibility -- a customer's wish
  is none of those). `SELECTED` is included because leaving it needs a reason code that would
  misstate why (`R-COMMIT-LOCK-INFEASIBLE`).
- **Renominate** only where the contract allows it (`renomination_allowed`), only for a committed or
  delivering obligation, and only when a declared re-nomination point is still ahead within the
  obligation's window. The request is queued for that point; the selector's re-nomination gate is
  the only place a commitment can change (02a S3.1). The customer never changes it directly.
- Anything already finished (`REJECTED`, `EXPIRED`, `FULFILLED`, `SHORTFALL`, `SETTLED`) is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from opengrid.contracts.state_machine import is_locked
from opengrid.core.models.engine import Contract, ObligationState

R_CUSTOMER_CANCEL_PRE_COMMIT = "R-CUSTOMER-CANCEL-PRE-COMMIT"
R_CUSTOMER_CANCEL_LOCKED_REVIEW = "R-CUSTOMER-CANCEL-LOCKED-REVIEW"
R_CUSTOMER_CANCEL_FINISHED = "R-CUSTOMER-CANCEL-FINISHED"
R_CUSTOMER_RENOM_NOT_ALLOWED = "R-CUSTOMER-RENOM-NOT-ALLOWED"
R_CUSTOMER_RENOM_NOT_COMMITTED = "R-CUSTOMER-RENOM-NOT-COMMITTED"
R_CUSTOMER_RENOM_NO_POINT = "R-CUSTOMER-RENOM-NO-POINT"
R_CUSTOMER_RENOM_QUEUED = "R-CUSTOMER-RENOM-QUEUED"

#: The state-machine edge a pre-commit cancel uses (02a S2.1 `OFFERED -> REJECTED`); the trace payload
#: records that the customer asked for it.
CANCEL_TRANSITION_REASON = "R-ADMIT-REJECT"

_CANCELLABLE_NOW: frozenset[ObligationState] = frozenset({"OFFERED"})
_SELECTED: ObligationState = "SELECTED"


class Disposition(StrEnum):
    APPLY_NOW = "APPLY_NOW"
    OPERATOR_REVIEW = "OPERATOR_REVIEW"
    QUEUE_FOR_RENOMINATION = "QUEUE_FOR_RENOMINATION"
    REFUSE = "REFUSE"


@dataclass(frozen=True, slots=True)
class RuleDecision:
    disposition: Disposition
    rule_code: str


def cancel_decision(state: ObligationState) -> RuleDecision:
    """How a customer's cancel of an obligation in `state` is handled (module docstring)."""
    if state in _CANCELLABLE_NOW:
        return RuleDecision(Disposition.APPLY_NOW, R_CUSTOMER_CANCEL_PRE_COMMIT)
    if state == _SELECTED or is_locked(state):
        return RuleDecision(Disposition.OPERATOR_REVIEW, R_CUSTOMER_CANCEL_LOCKED_REVIEW)
    return RuleDecision(Disposition.REFUSE, R_CUSTOMER_CANCEL_FINISHED)


def renominate_decision(
    state: ObligationState, *, renomination_allowed: bool, has_upcoming_point: bool
) -> RuleDecision:
    """How a customer's renomination of an obligation in `state` is handled (module docstring).
    `has_upcoming_point`: an unexercised re-nomination point for this obligation lies ahead, inside its
    window."""
    if not renomination_allowed:
        return RuleDecision(Disposition.REFUSE, R_CUSTOMER_RENOM_NOT_ALLOWED)
    if not is_locked(state):
        return RuleDecision(Disposition.REFUSE, R_CUSTOMER_RENOM_NOT_COMMITTED)
    if not has_upcoming_point:
        return RuleDecision(Disposition.REFUSE, R_CUSTOMER_RENOM_NO_POINT)
    return RuleDecision(Disposition.QUEUE_FOR_RENOMINATION, R_CUSTOMER_RENOM_QUEUED)


def penalty_terms(contract: Contract) -> dict[str, str | None]:
    """The contract's shortfall penalty terms (02a S1.2, S6.6), snapshotted onto a review request so the
    operator sees what the contract said when the customer asked."""
    return {
        "penalty_alpha": _text(contract.penalty_alpha),
        "penalty_beta": _text(contract.penalty_beta),
        "penalty_theta": _text(contract.penalty_theta),
    }


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
