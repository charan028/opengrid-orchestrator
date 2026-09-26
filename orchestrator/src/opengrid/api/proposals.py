"""In-process store for two-step confirmations (02b S7.1/S7.3): manual fleet commands and safe-stop
engagements. Step 1 (`POST .../command` or `POST .../safestop`) validates shape/role only and stores a
proposal; step 2 (`POST .../{proposal_id}/confirm`) is where the guardian/safestop check actually runs.

A single `og-api` process instance holds this in memory (MVP-S, single node, `Restart=always`): a
restart between step 1 and step 2 simply expires the proposal, which is the same externally-visible
outcome as the 60 s TTL (02b S7.1 "a proposal not confirmed within 60 s expires") -- there is no
silent failure mode, just "propose again."
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

PROPOSAL_TTL_S = 60.0


@dataclass(slots=True)
class Proposal[T]:
    proposal_id: UUID
    kind: str
    body: T
    summary: str
    proposer: str
    created_at: float = field(default_factory=time.monotonic)
    #: Confirmation window. 60 s by default (02b S7.1); a safe-stop proposal uses og-safestop's own
    #: [safestop].confirm_window_s so the API never accepts a confirm the broker already expired.
    ttl_s: float = PROPOSAL_TTL_S

    def expired(self, *, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) - self.created_at > self.ttl_s


class ProposalExpiredError(KeyError):
    """The proposal existed but its confirmation window (`Proposal.ttl_s`) has passed."""


class ProposalStore:
    """Keyed by `proposal_id`; each proposal is consumed (removed) on a successful `pop`."""

    def __init__(self) -> None:
        self._proposals: dict[UUID, Proposal[Any]] = {}

    def create[T](
        self, kind: str, body: T, summary: str, proposer: str, *, ttl_s: float = PROPOSAL_TTL_S
    ) -> Proposal[T]:
        self._sweep_expired()
        proposal: Proposal[T] = Proposal(
            proposal_id=uuid4(), kind=kind, body=body, summary=summary, proposer=proposer, ttl_s=ttl_s
        )
        self._proposals[proposal.proposal_id] = proposal
        return proposal

    def peek(self, proposal_id: UUID, *, kind: str) -> Proposal[Any]:
        proposal = self._proposals.get(proposal_id)
        if proposal is None or proposal.kind != kind:
            raise KeyError(proposal_id)
        if proposal.expired():
            del self._proposals[proposal_id]
            raise ProposalExpiredError(proposal_id)
        return proposal

    def pop(self, proposal_id: UUID, *, kind: str) -> Proposal[Any]:
        proposal = self.peek(proposal_id, kind=kind)
        del self._proposals[proposal_id]
        return proposal

    def _sweep_expired(self) -> None:
        now = time.monotonic()
        expired = [pid for pid, p in self._proposals.items() if p.expired(now=now)]
        for pid in expired:
            del self._proposals[pid]
