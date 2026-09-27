"""Q10 (lead 2026-09-27, owner to confirm): a UTILITY-initiated stop is released only when the utility LIFTS the
exact instruction that engaged it (a lift carrying that instruction id, recorded by og-safestop) AND two operators
then approve the normal release. Any other active BLOCK/ESTOP on the bank keeps it stopped."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from opengrid.core.models.mqtt import ScadaUtilityInstruction
from opengrid.guardian import repo
from opengrid.guardian.ports import L2Instruction
from opengrid.integrations.instructions import DesiredInstruction, InstructionTracker
from opengrid.safestop.l2_intake import handle_instruction, instruction_id_from_reason, l2_reason

from .conftest import NOW
from .test_repo import FakeCursor, FakePool
from .test_stop_release import FakeStopRelease, _check, _engaged, _request, _service

BLOCK_ID = UUID("11111111-1111-4111-8111-111111111111")
ESTOP_ID = UUID("22222222-2222-4222-8222-222222222222")


def _utility_stop(instruction_id: UUID, kind: str = "BLOCK", **overrides):
    reason = f"L2 {kind} {instruction_id} from SCENARIO_ANOMALY"
    return _engaged(initiator_kind="UTILITY", reason=reason, **overrides)


LIFTED_BEFORE_APPROVAL = NOW - timedelta(seconds=45)  # approval is at NOW - 30 s


# --- the pure check ------------------------------------------------------------------------------------------------


def test_the_reason_names_the_instruction_that_engaged_the_stop():
    instruction = ScadaUtilityInstruction(
        instruction_id=BLOCK_ID, bank_id="bank-001", kind="BLOCK", issued_at=NOW, issued_by="SCENARIO_ANOMALY"
    )
    assert instruction_id_from_reason(l2_reason(instruction)) == str(BLOCK_ID)
    assert instruction_id_from_reason("operator drill") is None
    assert instruction_id_from_reason(None) is None
    assert instruction_id_from_reason(f"L2 LIMIT {BLOCK_ID} from x") is None


def test_block_then_the_utility_lift_with_its_id_then_the_two_person_release_passes():
    outcome = _check(engaged=[_utility_stop(BLOCK_ID)], utility_lifts={str(BLOCK_ID): LIFTED_BEFORE_APPROVAL})
    assert outcome.ok


@pytest.mark.parametrize(
    ("engaged", "lifts", "reason"),
    [
        # the operators' release without the utility's lift
        ([_utility_stop(BLOCK_ID)], {}, "UTILITY_STOP_NOT_LIFTED_BY_UTILITY"),
        # the utility lifted a DIFFERENT instruction
        (
            [_utility_stop(BLOCK_ID)],
            {str(uuid4()): LIFTED_BEFORE_APPROVAL},
            "UTILITY_STOP_NOT_LIFTED_BY_UTILITY",
        ),
        # an ESTOP engaged its own stop on the bank and is still active (not lifted)
        (
            [_utility_stop(BLOCK_ID), _utility_stop(ESTOP_ID, "ESTOP")],
            {str(BLOCK_ID): LIFTED_BEFORE_APPROVAL},
            "UTILITY_STOP_NOT_LIFTED_BY_UTILITY",
        ),
        # lifted only after the operators approved: the utility must initiate
        (
            [_utility_stop(BLOCK_ID)],
            {str(BLOCK_ID): NOW - timedelta(seconds=5)},
            "UTILITY_LIFT_AFTER_APPROVAL",
        ),
        # a utility stop whose reason names no instruction can never be matched to a lift
        ([_engaged(initiator_kind="UTILITY", reason="?")], {}, "UTILITY_STOP_INSTRUCTION_UNKNOWN"),
    ],
)
def test_a_utility_stop_stays_stopped_unless_its_own_instruction_was_lifted_first(engaged, lifts, reason):
    outcome = _check(engaged=engaged, utility_lifts=lifts)
    assert not outcome.ok and outcome.reason == reason


def test_an_active_estop_on_the_bank_still_blocks_after_the_lift():
    outcome = _check(
        engaged=[_utility_stop(BLOCK_ID)],
        utility_lifts={str(BLOCK_ID): LIFTED_BEFORE_APPROVAL},
        active_instruction_kinds=["ESTOP"],
    )
    assert not outcome.ok and outcome.reason == "STOP_REASON_ACTIVE_L2_INSTRUCTION"


# --- the service, on its own reads ------------------------------------------------------------------------------


async def test_the_service_signs_the_release_after_the_utility_lift(fakes, signing_seed):
    port = FakeStopRelease(engaged=[_utility_stop(BLOCK_ID)])
    port.lifts = {str(BLOCK_ID): LIFTED_BEFORE_APPROVAL}
    events = await _service(fakes, signing_seed, port=port).evaluate_and_sign_stop_release(_request())
    assert events is not None and len(events) == 1


async def test_the_service_refuses_the_operators_release_without_the_lift(fakes, signing_seed):
    port = FakeStopRelease(engaged=[_utility_stop(BLOCK_ID)])
    port.lifts = {}
    assert await _service(fakes, signing_seed, port=port).evaluate_and_sign_stop_release(_request()) is None
    assert fakes.trace.release_verdicts[-1][1]["reason"] == "UTILITY_STOP_NOT_LIFTED_BY_UTILITY"


async def test_the_service_refuses_while_an_estop_is_active_even_after_the_lift(fakes, signing_seed):
    fakes.l2_instructions.active["bank-001"] = L2Instruction(kind="ESTOP", limit_kw=None)
    port = FakeStopRelease(engaged=[_utility_stop(BLOCK_ID)])
    port.lifts = {str(BLOCK_ID): LIFTED_BEFORE_APPROVAL}
    assert await _service(fakes, signing_seed, port=port).evaluate_and_sign_stop_release(_request()) is None
    assert fakes.trace.release_verdicts[-1][1]["reason"] == "STOP_REASON_ACTIVE_L2_INSTRUCTION"


async def test_the_guardian_reads_the_first_recorded_lift_per_instruction():
    lifted_at = NOW - timedelta(seconds=40)
    cursor = FakeCursor([[(str(BLOCK_ID), lifted_at)]])
    port = repo.PgStopReleasePort(FakePool(cursor))
    assert await port.utility_lifts(["bank-001"]) == {str(BLOCK_ID): lifted_at}
    sql = " ".join(cursor.executed[0][0].split())
    assert (
        "t.event_class = 'SAFE_STOP'" in sql
        and "t.payload ->> 'l2' = 'LIFT'" in sql
        and "min(t.created_at)" in sql
    )
    assert await port.utility_lifts([]) == {}


# --- og-safestop records the utility's lift (never releases) ------------------------------------------------------


def _instruction(**overrides) -> dict:
    base = {
        "instruction_id": str(uuid4()),
        "bank_id": "bank-007",
        "kind": "BLOCK",
        "limit_kw": None,
        "issued_at": "2026-09-27T04:40:00Z",
        "expires_at": "2026-09-27T04:40:00Z",  # a lift: already expired
        "issued_by": "SCENARIO_ANOMALY",
        "lifts_instruction_id": str(BLOCK_ID),
    }
    base.update(overrides)
    return base


async def _handle(message: dict, recorded: list, *, handled: set | None = None, fail: bool = False) -> str:
    async def record(bank_id, lifted, lift_id, issued_by) -> None:
        if fail:
            raise RuntimeError("trace down")
        recorded.append((bank_id, lifted, lift_id, issued_by))

    async def never(*_a):
        raise AssertionError("a lift never engages")

    return await handle_instruction(
        message,
        now=datetime(2026, 9, 27, 4, 41, tzinfo=UTC),
        handled=handled if handled is not None else set(),
        engage_fn=never,
        already_acted_fn=never,
        record_lift_fn=record,
        topic_bank_id="bank-007",
    )


async def test_safestop_records_a_lift_that_names_its_instruction():
    recorded: list = []
    handled: set = set()
    message = _instruction()
    assert await _handle(message, recorded, handled=handled) == "LIFT_RECORDED"
    assert recorded == [("bank-007", BLOCK_ID, UUID(message["instruction_id"]), "SCENARIO_ANOMALY")]
    assert await _handle(message, recorded, handled=handled) == "DUPLICATE"  # QoS 1 re-delivery
    assert len(recorded) == 1


async def test_an_expired_instruction_without_a_lift_id_is_only_ignored():
    recorded: list = []
    assert await _handle(_instruction(lifts_instruction_id=None), recorded) == "IGNORED_EXPIRED"
    assert recorded == []


async def test_a_lift_that_cannot_be_recorded_is_retried_on_redelivery():
    recorded: list = []
    handled: set = set()
    message = _instruction()
    assert await _handle(message, recorded, handled=handled, fail=True) == "FAILED"
    assert await _handle(message, recorded, handled=handled) == "LIFT_RECORDED"


async def test_safestop_traces_the_lift_and_refuses_without_a_trace():
    from opengrid.core.crypto import generate_keypair
    from opengrid.safestop.keys import StopSigningKey
    from opengrid.safestop.service import SafestopService

    class _Trace:
        def __init__(self) -> None:
            self.rows: list = []

        async def append(self, *args, **kwargs):
            self.rows.append((args, kwargs))

    seed, _ = generate_keypair()
    trace = _Trace()
    service = SafestopService(StopSigningKey("safestop-test", seed), None, None, trace)  # type: ignore[arg-type]
    lift_id = uuid4()
    await service.record_utility_lift("bank-007", BLOCK_ID, lift_id, "SCENARIO_ANOMALY")
    (args, kwargs) = trace.rows[0]
    assert args[2] == "SAFE_STOP" and kwargs["reason_codes"] == ["SAFE_STOP_L2_LIFT"]
    assert args[3] == {
        "l2": "LIFT",
        "bank_id": "bank-007",
        "lifted_instruction_id": str(BLOCK_ID),
        "lift_instruction_id": str(lift_id),
        "issued_by": "SCENARIO_ANOMALY",
    }
    service.trace = None
    with pytest.raises(RuntimeError):
        await service.record_utility_lift("bank-007", BLOCK_ID, lift_id, "x")


# --- every lift the orchestrator's own protocol adapters emit names the instruction it ends ----------------------


def test_the_protocol_tracker_lift_names_the_block_it_ends():
    tracker = InstructionTracker(source="dnp3", issued_by="utility-ems")
    block = tracker.update(
        "bank-007", DesiredInstruction.from_levels(estop=False, block=True, limit_kw=None), now=NOW
    )
    lift = tracker.update("bank-007", None, now=NOW + timedelta(seconds=60))
    assert block is not None and lift is not None
    assert block.lifts_instruction_id is None
    assert lift.lifts_instruction_id == block.instruction_id and lift.expires_at == lift.issued_at
