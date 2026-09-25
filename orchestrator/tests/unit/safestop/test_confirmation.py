from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from opengrid.safestop.confirmation import (
    ConfirmationBroker,
    ProposalExpiredError,
    UnknownProposalError,
)


class _FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def test_propose_then_confirm_within_window_succeeds():
    clock = _FakeClock(datetime(2026, 9, 26, 18, 0, 0, tzinfo=UTC))
    broker = ConfirmationBroker(confirm_window_s=30, clock=clock)

    proposal = broker.propose(scope="BANK", scope_ref="bank-07", reason="drill", initiator_ref="op:a")
    clock.advance(5)
    confirmed = broker.confirm(proposal.proposal_id)

    assert confirmed.proposal_id == proposal.proposal_id
    assert confirmed.scope == "BANK"
    assert broker.pending_count() == 0


def test_confirm_unknown_proposal_raises():
    broker = ConfirmationBroker()
    with pytest.raises(UnknownProposalError):
        broker.confirm(__import__("uuid").uuid4())


def test_confirm_after_window_raises_and_consumes_proposal():
    clock = _FakeClock(datetime(2026, 9, 26, 18, 0, 0, tzinfo=UTC))
    broker = ConfirmationBroker(confirm_window_s=10, clock=clock)
    proposal = broker.propose(scope="ZONE", scope_ref="LZ_NORTH", reason="drill", initiator_ref="op:a")

    clock.advance(11)
    with pytest.raises(ProposalExpiredError):
        broker.confirm(proposal.proposal_id)

    # a second confirm attempt on the same id is now UnknownProposalError -- it was consumed
    with pytest.raises(UnknownProposalError):
        broker.confirm(proposal.proposal_id)


def test_a_single_propose_never_engages_anything():
    """TS-10-03: 'a single click never stops the fleet' -- propose() alone must not be usable to skip
    confirm(); it only returns a proposal, it does not call engage."""
    broker = ConfirmationBroker()
    proposal = broker.propose(scope="FLEET", scope_ref="", reason="drill", initiator_ref="op:a")
    assert broker.pending_count() == 1
    assert proposal.proposal_id is not None  # still requires a separate confirm() call


def test_discard_expired_removes_only_stale_proposals():
    clock = _FakeClock(datetime(2026, 9, 26, 18, 0, 0, tzinfo=UTC))
    broker = ConfirmationBroker(confirm_window_s=10, clock=clock)
    stale = broker.propose(scope="BANK", scope_ref="bank-01", reason="a", initiator_ref="op:a")
    clock.advance(11)
    fresh = broker.propose(scope="BANK", scope_ref="bank-02", reason="b", initiator_ref="op:a")

    dropped = broker.discard_expired()

    assert dropped == 1
    assert broker.pending_count() == 1
    with pytest.raises(UnknownProposalError):
        broker.confirm(stale.proposal_id)
    broker.confirm(fresh.proposal_id)  # still valid


def test_propose_accepts_externally_supplied_proposal_id():
    import uuid

    broker = ConfirmationBroker()
    given_id = uuid.uuid4()
    proposal = broker.propose(
        scope="BANK", scope_ref="bank-07", reason="drill", initiator_ref="op:a", proposal_id=given_id
    )
    assert proposal.proposal_id == given_id
