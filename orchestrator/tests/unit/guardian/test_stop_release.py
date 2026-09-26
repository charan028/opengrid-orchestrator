"""K8 stop RELEASE, guardian side: the precondition check, the signed-event builder, the service method
over in-memory fakes, the Postgres adapter's row mapping and the main-loop hand-off."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from opengrid.core.crypto import private_key_from_seed, verify_payload
from opengrid.guardian import main as guardian_main
from opengrid.guardian import repo, stop_release
from opengrid.guardian.config import GuardianConfig
from opengrid.guardian.ports import EngagedStop, L2Instruction, ReleaseRequest

from .conftest import NOW, service_with
from .test_repo import FakeCursor, FakePool

OPERATORS = frozenset({"alice", "bob"})
ENGAGE_ID = UUID("00000000-0000-4000-8000-0000000000e1")


def _request(**overrides: Any) -> ReleaseRequest:
    base: dict[str, Any] = {
        "operator_action_id": uuid4(),
        "requested_by": "alice",
        "approved_by": "bob",
        "scope_kind": "BANK",
        "scope_ref": "bank-001",
        "reason": "feeder repaired",
        "requested_at": NOW - timedelta(seconds=60),
        "approved_at": NOW - timedelta(seconds=30),
        "trace_id": uuid4(),
    }
    base.update(overrides)
    return ReleaseRequest(**base)


def _engaged(**overrides: Any) -> EngagedStop:
    base: dict[str, Any] = {
        "stop_id": ENGAGE_ID,
        "initiator_kind": "SAFESTOP_AUTHORITY",
        "engaged_at": NOW - timedelta(hours=1),
    }
    base.update(overrides)
    return EngagedStop(**base)


def _check(request: ReleaseRequest | None = None, **overrides: Any):
    kwargs: dict[str, Any] = {
        "now": NOW,
        "engaged": [_engaged()],
        "active_instruction_kinds": [],
        "authorised_operators": OPERATORS,
        "approval_max_age_s": 300.0,
        "max_clock_skew_s": 5.0,
        "request_traced": True,
    }
    kwargs.update(overrides)
    return stop_release.check_stop_release(request or _request(), **kwargs)


# --- the precondition check ------------------------------------------------------------------------------


def test_a_tier2_approved_release_of_a_cleared_stop_passes():
    assert _check().ok


@pytest.mark.parametrize(
    ("request_overrides", "check_overrides", "reason"),
    [
        ({}, {"authorised_operators": frozenset()}, "NO_AUTHORISED_OPERATORS_CONFIGURED"),
        ({"approved_by": None}, {}, "NOT_APPROVED"),
        ({"approved_by": "  "}, {}, "NOT_APPROVED"),
        ({"approved_by": " ALICE "}, {}, "SAME_OPERATOR"),
        ({"approved_by": "mallory"}, {}, "OPERATOR_NOT_AUTHORISED"),
        ({"requested_by": "mallory"}, {}, "OPERATOR_NOT_AUTHORISED"),
        ({"approved_at": None}, {}, "APPROVAL_STALE"),
        ({"approved_at": NOW - timedelta(seconds=301)}, {}, "APPROVAL_STALE"),
        ({"approved_at": NOW + timedelta(seconds=30)}, {}, "APPROVAL_STALE"),
        ({"approved_at": NOW - timedelta(seconds=90)}, {}, "APPROVAL_STALE"),  # before it was requested
        ({}, {"request_traced": False}, "REQUEST_NOT_TRACED"),
        ({}, {"engaged": []}, "NOT_ENGAGED"),
        ({}, {"engaged": [_engaged(engaged_at=NOW - timedelta(seconds=10))]}, "STOP_ENGAGED_AFTER_APPROVAL"),
        ({}, {"engaged": [_engaged(initiator_kind="UTILITY")]}, "UTILITY_STOP_NOT_OPERATOR_RELEASABLE"),
        ({}, {"active_instruction_kinds": ["ESTOP"]}, "STOP_REASON_ACTIVE_L2_INSTRUCTION"),
        ({}, {"active_instruction_kinds": ["LIMIT", "BLOCK"]}, "STOP_REASON_ACTIVE_L2_INSTRUCTION"),
    ],
)
def test_every_unmet_precondition_refuses(request_overrides, check_overrides, reason):
    outcome = _check(_request(**request_overrides), **check_overrides)
    assert not outcome.ok and outcome.rule_id == "K8-RELEASE" and outcome.reason == reason


def test_a_limit_instruction_does_not_keep_the_stop_in_force():
    assert _check(active_instruction_kinds=["LIMIT"]).ok


# --- the signed events -------------------------------------------------------------------------------------


def test_one_release_per_outstanding_engage_on_its_own_topic_signed_over_the_wire_fields(signing_seed):
    other = UUID("00000000-0000-4000-8000-0000000000e2")
    events = stop_release.build_release_events(
        _request(),
        [_engaged(), _engaged(stop_id=other)],
        seed=signing_seed,
        key_id="guardian-2026a",
        issued_at=NOW,
    )
    public = private_key_from_seed(signing_seed).public_key().public_bytes_raw()

    assert [e.stop_id for e in events] == [ENGAGE_ID, other]
    for event in events:
        wire = json.loads(event.model_dump_json())
        assert (wire["action"], wire["scope"], wire["scope_id"]) == ("RELEASE", "bank", "bank-001")
        assert (wire["issued_by"], wire["approver_ref"], wire["key_id"]) == ("alice", "bob", "guardian-2026a")
        signed = {k: v for k, v in wire.items() if k not in ("key_id", "signature")}
        assert verify_payload(public, signed, wire["signature"])
        assert not verify_payload(public, {**signed, "scope_id": "bank-002"}, wire["signature"])


def test_fleet_release_has_no_scope_id(signing_seed):
    (event,) = stop_release.build_release_events(
        _request(scope_kind="FLEET", scope_ref="FLEET"),
        [_engaged()],
        seed=signing_seed,
        key_id="guardian-x",
        issued_at=NOW,
    )
    assert (event.scope, event.scope_id) == ("fleet", None)


# --- the service method --------------------------------------------------------------------------------------


class FakeStopRelease:
    def __init__(self, engaged: list[EngagedStop] | None = None, banks: list[str] | None = None) -> None:
        self.engaged = engaged if engaged is not None else [_engaged()]
        self.banks = banks if banks is not None else ["bank-001"]

    async def pending_requests(self, *, max_age_s: float) -> list[ReleaseRequest]:
        return []

    async def outstanding_engages(self, scope_kind: str, scope_ref: str) -> list[EngagedStop]:
        return list(self.engaged)

    async def banks_in_scope(self, scope_kind: str, scope_ref: str) -> list[str]:
        return list(self.banks)


def _service(fakes, signing_seed, *, port: FakeStopRelease | None = None, operators=OPERATORS):
    fakes_port = port if port is not None else FakeStopRelease()
    config = GuardianConfig(key_path="", stop_release_authorised_operators=operators)
    service = service_with(fakes, config, signing_seed)
    service.ports = replace(fakes.as_ports(), stop_release=fakes_port)
    return service


async def test_service_signs_and_traces_a_valid_release(fakes, signing_seed):
    request = _request()
    events = await _service(fakes, signing_seed).evaluate_and_sign_stop_release(request)

    assert events is not None and [e.stop_id for e in events] == [ENGAGE_ID]
    (action_id, payload) = fakes.trace.release_verdicts[-1]
    assert action_id == request.operator_action_id and payload["outcome"] == "SIGNED"
    assert payload["events"] == [e.model_dump(mode="json") for e in events]
    assert fakes.trace.preimage_refs_checked == [request.trace_id]


async def test_service_refuses_while_a_utility_estop_is_active_on_the_scope(fakes, signing_seed):
    fakes.l2_instructions.active["bank-001"] = L2Instruction(kind="ESTOP", limit_kw=None)
    assert await _service(fakes, signing_seed).evaluate_and_sign_stop_release(_request()) is None
    assert fakes.trace.release_verdicts[-1][1]["reason"] == "STOP_REASON_ACTIVE_L2_INSTRUCTION"


async def test_service_refuses_when_the_request_was_never_traced(fakes, signing_seed):
    fakes.trace.preimage_exists = False
    assert await _service(fakes, signing_seed).evaluate_and_sign_stop_release(_request()) is None
    assert fakes.trace.release_verdicts[-1][1]["reason"] == "REQUEST_NOT_TRACED"


async def test_service_refuses_on_a_bad_clock(fakes, signing_seed):
    fakes.clock.offset_ms = math.inf
    assert await _service(fakes, signing_seed).evaluate_and_sign_stop_release(_request()) is None
    assert fakes.trace.release_verdicts[-1][1]["rule_id"] == "G-20"


async def test_service_refuses_when_the_release_path_is_not_wired(fakes, signing_seed, guardian_config):
    service = service_with(fakes, guardian_config, signing_seed)
    assert await service.evaluate_and_sign_stop_release(_request()) is None
    assert fakes.trace.release_verdicts[-1][1]["reason"] == "STOP_RELEASE_NOT_WIRED"


async def test_service_withholds_a_release_it_cannot_trace(fakes, signing_seed):
    from .conftest import FailingTrace

    fakes.trace = FailingTrace()
    assert await _service(fakes, signing_seed).evaluate_and_sign_stop_release(_request()) is None


async def test_service_refuses_with_no_authorised_operators_configured(fakes, signing_seed):
    assert (
        await _service(fakes, signing_seed, operators=frozenset()).evaluate_and_sign_stop_release(_request())
        is None
    )


# --- the Postgres adapter ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("BANK:bank-001", ("BANK", "bank-001")),
        ("zone:LZ_NORTH", ("ZONE", "LZ_NORTH")),
        ("FLEET:anything", ("FLEET", "FLEET")),
        ("FLEET:", ("FLEET", "FLEET")),
        ("BANK:", None),
        ("bank-001", None),
        ("HUB:hub-1", None),
        (None, None),
    ],
)
def test_parse_release_target(target, expected):
    assert repo.parse_release_target(target) == expected


async def test_pending_requests_map_rows_and_skip_unparsable_targets():
    action_id, trace_id = uuid4(), uuid4()
    requested, approved = NOW - timedelta(seconds=60), NOW - timedelta(seconds=30)
    cursor = FakeCursor(
        [
            [
                (action_id, "alice", "bob", "ZONE:LZ_NORTH", "cleared", requested, approved, trace_id),
                (uuid4(), "alice", "bob", "garbage", "x", requested, approved, None),
            ]
        ]
    )
    requests = await repo.PgStopReleasePort(FakePool(cursor)).pending_requests(max_age_s=300.0)

    assert requests == [
        _request(
            operator_action_id=action_id,
            scope_kind="ZONE",
            scope_ref="LZ_NORTH",
            reason="cleared",
            requested_at=requested,
            approved_at=approved,
            trace_id=trace_id,
        )
    ]
    sql, params = cursor.executed[0]
    assert "SAFE_STOP_RELEASE" in sql and "'TIER2'" in sql and "NOT EXISTS" in sql and "'STOP_RELEASE'" in sql
    assert params == {"max_age_s": 300.0}


async def test_outstanding_engages_banks_and_unpublished_events():
    engaged_at = NOW - timedelta(hours=1)
    event = {"stop_id": str(ENGAGE_ID), "action": "RELEASE", "signature": "s"}
    cursor = FakeCursor(
        [[(ENGAGE_ID, "SAFESTOP_AUTHORITY", engaged_at)], [("bank-001",), ("bank-005",)], [(event,)]]
    )
    port = repo.PgStopReleasePort(FakePool(cursor))

    assert await port.outstanding_engages("ZONE", "LZ_NORTH") == [_engaged(engaged_at=engaged_at)]
    assert await port.banks_in_scope("ZONE", "LZ_NORTH") == ["bank-001", "bank-005"]
    assert await port.unpublished_release_events(max_age_s=300.0) == [event]
    assert "action = 'RELEASE'" in cursor.executed[0][0]
    assert "zone = %(scope_ref)s" in cursor.executed[1][0]
    assert "s.signature = ev ->> 'signature'" in cursor.executed[2][0]


async def test_hand_to_safestop_notifies_the_safestop_request_channel():
    from opengrid.safestop.pg_backend import REQUEST_CHANNEL

    cursor = FakeCursor([None])
    pool = FakePool(cursor)
    await repo.PgStopReleasePort(pool).hand_to_safestop({"stop_id": "x"})

    sql, params = cursor.executed[0]
    assert "pg_notify" in sql and params["channel"] == REQUEST_CHANNEL
    assert json.loads(params["payload"]) == {"action": "PUBLISH_RELEASE", "event": {"stop_id": "x"}}
    assert pool._conn.committed


# --- main-loop hand-off ----------------------------------------------------------------------------------


class _RecordingPort:
    def __init__(self, requests: list[ReleaseRequest], unpublished: list[dict[str, Any]]) -> None:
        self.requests, self.unpublished = requests, unpublished
        self.handed: list[dict[str, Any]] = []

    async def pending_requests(self, *, max_age_s: float) -> list[ReleaseRequest]:
        return list(self.requests)

    async def unpublished_release_events(self, *, max_age_s: float) -> list[dict[str, Any]]:
        return list(self.unpublished)

    async def hand_to_safestop(self, event: dict[str, Any]) -> None:
        self.handed.append(event)


class _RecordingService:
    def __init__(self) -> None:
        self.seen: list[ReleaseRequest] = []

    async def evaluate_and_sign_stop_release(self, request: ReleaseRequest) -> None:
        self.seen.append(request)


async def test_process_pending_stop_releases_decides_requests_then_hands_every_unpublished_event():
    port = _RecordingPort([_request(), _request()], [{"signature": "a"}, {"signature": "b"}])
    service = _RecordingService()

    handed = await guardian_main.process_pending_stop_releases(
        port=port,  # type: ignore[arg-type]
        service=service,  # type: ignore[arg-type]
        config=GuardianConfig(key_path=""),
    )

    assert len(service.seen) == 2 and handed == 2
    assert port.handed == [{"signature": "a"}, {"signature": "b"}]
