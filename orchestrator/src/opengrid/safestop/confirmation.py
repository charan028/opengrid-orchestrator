"""Two-step confirmation for a safe-stop request (02b S8, `POST /og/api/safestop` then
`/{proposal_id}/confirm`). Pure in-memory state machine, no I/O, so it is trivially unit-testable and
carries no dependency on `opengrid.engine`/`opengrid.guardian` (K8 import isolation).

`opengrid.safestop.main` owns the actual transport (Postgres LISTEN/NOTIFY, see
`orchestrator/src/opengrid/safestop/README.md`): a caller PROPOSEs a stop, receives a `proposal_id`,
then must CONFIRM the same `proposal_id` within `confirm_window_s` before `engage()` is ever called --
a single message can never stop the fleet (TS-10-03).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from opengrid.safestop.events import Scope

DEFAULT_CONFIRM_WINDOW_S = 30


class UnknownProposalError(KeyError):
    """confirm()/expire housekeeping referenced a proposal_id that was never proposed (or already used)."""


class ProposalExpiredError(Exception):
    """confirm() called after `confirm_window_s` elapsed since propose() -- the proposal must be redone."""


@dataclass(frozen=True, slots=True)
class StopProposal:
    proposal_id: UUID
    scope: Scope
    scope_ref: str
    reason: str
    initiator_ref: str
    proposed_at: datetime


@dataclass
class ConfirmationBroker:
    """Holds pending proposals in memory. `og-safestop` is otherwise stateless (per TS-C-03b: "no state
    to rebuild"), so a proposal lost on restart simply requires the operator to re-arm -- fail closed,
    never fail open."""

    confirm_window_s: int = DEFAULT_CONFIRM_WINDOW_S
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    _pending: dict[UUID, StopProposal] = field(default_factory=dict)

    def propose(
        self,
        *,
        scope: Scope,
        scope_ref: str,
        reason: str,
        initiator_ref: str,
        proposal_id: UUID | None = None,
    ) -> StopProposal:
        """Step 1: arm a stop request. Does not engage anything. `proposal_id` lets the caller (the
        API process, over the LISTEN/NOTIFY request channel) choose the id up front so it can hand the
        same id back to the operator for step 2 without a synchronous reply from `og-safestop`."""
        proposal = StopProposal(
            proposal_id=proposal_id or uuid4(),
            scope=scope,
            scope_ref=scope_ref,
            reason=reason,
            initiator_ref=initiator_ref,
            proposed_at=self.clock(),
        )
        self._pending[proposal.proposal_id] = proposal
        return proposal

    def confirm(self, proposal_id: UUID) -> StopProposal:
        """Step 2: confirm the same proposal. Consumes it (a proposal confirms at most once) and
        returns it for the caller to pass to `engage()`. Raises if unknown or expired."""
        proposal = self._pending.pop(proposal_id, None)
        if proposal is None:
            raise UnknownProposalError(str(proposal_id))
        age_s = (self.clock() - proposal.proposed_at).total_seconds()
        if age_s > self.confirm_window_s:
            raise ProposalExpiredError(f"proposal {proposal_id} expired after {age_s:.1f}s")
        return proposal

    def discard_expired(self) -> int:
        """Housekeeping: drop proposals past their confirm window that were never confirmed. Returns
        the count dropped."""
        now = self.clock()
        expired = [
            pid
            for pid, proposal in self._pending.items()
            if (now - proposal.proposed_at).total_seconds() > self.confirm_window_s
        ]
        for pid in expired:
            del self._pending[pid]
        return len(expired)

    def pending_count(self) -> int:
        return len(self._pending)
