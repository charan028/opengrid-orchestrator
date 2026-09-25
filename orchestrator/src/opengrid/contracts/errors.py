"""Exception types for `opengrid.contracts` (02a S1-S2).

Every exception here carries a `.reason_code` -- the string that gets written into `obligation
.last_reason_code` / `opportunity.reason_code` and traced by `opengrid.trace`, per BUILD.md S5a
("Explicit exception types") and ES04-S05 ("every admission rejection... traced with a reason code").
"""

from __future__ import annotations


class AdmissionError(Exception):
    """Raised by `admit()` when a call cannot even reach `OFFERED` (02a S2.1's `[*] -> REJECTED`
    branch): unknown/inactive contract, invalid window, invalid quantity, or the product rule can
    never yield a non-zero tradable quantity (`opengrid.core.products.is_feasible`)."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class IllegalTransitionError(Exception):
    """Raised when a caller attempts an obligation/opportunity state transition that the 02a S2.1
    state machine does not allow, or that lacks a reason code the transition requires (e.g. a
    `DELIVERING -> SHORTFALL` write missing one of the K13 override codes)."""

    def __init__(self, *, from_state: str, to_state: str, reason_code: str | None) -> None:
        message = f"illegal transition {from_state} -> {to_state} (reason_code={reason_code!r})"
        super().__init__(message)
        self.from_state = from_state
        self.to_state = to_state
        self.reason_code = reason_code


class ConcurrentUpdateError(Exception):
    """Raised when an obligation/opportunity update's expected `version` no longer matches the
    stored row -- another writer moved it first (optimistic-lock conflict, 02a S1.5's `version`)."""

    def __init__(self, obligation_id: object, expected_version: int) -> None:
        super().__init__(f"obligation {obligation_id} is no longer at version {expected_version}")
        self.obligation_id = obligation_id
        self.expected_version = expected_version


class RenominationError(Exception):
    """Raised by `renomination` module calls: reselection attempted outside a declared
    re-nomination point, or an unknown/already-exercised point id (K13)."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
