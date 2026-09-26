"""K6 command freshness (00-invariants.md K6; G-13): sequence and epoch only ever move forward, inside a lease."""

from __future__ import annotations

from datetime import timedelta

from hypothesis import given
from hypothesis import strategies as st

from opengrid.core.timeutil import check_command_freshness

from .support import NOW, Signer, World, evaluate, make_hub, make_proposal

SIGNER = Signer.new()
LEASE = timedelta(seconds=10)
_counter = st.integers(min_value=0, max_value=6)


def _check(epoch: int, seq: int, last: tuple[int, int], *, now=NOW):
    return check_command_freshness(
        epoch=epoch,
        seq=seq,
        last_accepted_epoch=last[0],
        last_accepted_seq=last[1],
        issued_at=NOW,
        expires_at=NOW + LEASE,
        now=now,
    )


@given(st.lists(st.tuples(_counter, _counter), max_size=60))
def test_k06_accepted_commands_strictly_advance_and_everything_else_is_rejected(stream):
    last = (0, 0)
    accepted = []
    for epoch, seq in stream:
        result = _check(epoch, seq, last)
        assert result.ok == ((epoch, seq) > last)
        if result.ok:
            last = (epoch, seq)
            accepted.append(last)

    assert accepted == sorted(set(accepted))


@given(_counter, _counter)
def test_k06_a_replayed_command_is_rejected(epoch, seq):
    assert _check(epoch, seq, (epoch, seq)).reason == "STALE_SEQ"


@given(st.integers(min_value=-30, max_value=40))
def test_k06_a_fresh_command_is_accepted_only_inside_its_lease(offset_s):
    result = _check(1, 1, (0, 0), now=NOW + timedelta(seconds=offset_s))

    assert result.ok == (0 <= offset_s <= LEASE.total_seconds())


@given(_counter, _counter, _counter, _counter)
def test_k06_guardian_signs_only_commands_newer_than_its_last_accepted_lease(
    epoch, seq, last_epoch, last_seq
):
    world = World(proposal=make_proposal(epoch=epoch, seq=seq), hub=make_hub())
    world.last_lease = (last_epoch, last_seq)

    verdict = evaluate(world, SIGNER)

    assert (verdict.outcome == "PASS") == ((epoch, seq) > (last_epoch, last_seq))
