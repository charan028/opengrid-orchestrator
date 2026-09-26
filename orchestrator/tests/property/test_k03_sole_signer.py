"""K3 sole signer (00-invariants.md K3): a batch is signed only if every guardian check passes."""

from __future__ import annotations

from datetime import UTC
from uuid import uuid4

from hypothesis import given
from hypothesis import strategies as st

from opengrid.core.crypto import verify_payload
from opengrid.core.models.engine import Verdict
from opengrid.guardian.ports import L2Instruction

from .support import Signer, World, evaluate, make_hub, passing_world

SIGNER = Signer.new()


def _stale_seq(world: World) -> None:
    world.last_lease = (world.proposal.epoch, world.proposal.seq)


def _no_preimage(world: World) -> None:
    world.preimage_exists = False


def _clock_skew(world: World) -> None:
    world.offset_ms = 10_000.0


def _l2_block(world: World) -> None:
    world.l2 = L2Instruction("BLOCK", None)


def _stopped(world: World) -> None:
    world.stopped = True


def _low_soc(world: World) -> None:
    world.hub = make_hub(soc_kwh=8.0)


def _stale_ledger(world: World) -> None:
    world.ledger_version = 2


FAULTS = {
    "stale_seq": _stale_seq,
    "no_preimage": _no_preimage,
    "clock_skew": _clock_skew,
    "l2_block": _l2_block,
    "stopped": _stopped,
    "low_soc": _low_soc,
    "stale_ledger": _stale_ledger,
}


def _signed_payload(verdict: Verdict) -> dict[str, object]:
    assert verdict.signed_at is not None
    return {
        "command_batch_id": str(verdict.command_batch_id),
        "outcome": verdict.outcome,
        "vetoed_rule_ids": verdict.vetoed_rule_ids,
        "inputs_hash": verdict.inputs_hash,
        "signed_at": verdict.signed_at.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    }


@given(st.sets(st.sampled_from(sorted(FAULTS))))
def test_k03_signature_exists_if_and_only_if_no_check_failed(faults):
    world = passing_world()
    for fault in faults:
        FAULTS[fault](world)

    verdict = evaluate(world, SIGNER)

    assert (verdict.outcome == "PASS") == (not faults)
    assert (verdict.signature is not None) == (verdict.outcome == "PASS")
    assert (verdict.signed_at is not None) == (verdict.outcome == "PASS")


@given(st.integers(min_value=0, max_value=3))
def test_k03_a_pass_signature_verifies_only_for_its_own_payload_and_key(tampered_field):
    verdict = evaluate(passing_world(), SIGNER)
    payload = _signed_payload(verdict)
    assert verdict.signature is not None
    assert verify_payload(SIGNER.public, payload, verdict.signature)

    tampered = dict(payload)
    tampered[["command_batch_id", "outcome", "inputs_hash", "signed_at"][tampered_field]] = str(uuid4())

    assert not verify_payload(SIGNER.public, tampered, verdict.signature)
    assert not verify_payload(Signer.new().public, payload, verdict.signature)
