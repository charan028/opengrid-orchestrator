"""D-35 ERCOT AS instruction poller (`opengrid.contracts.as_deployment_poll`) with fake ports: idempotency,
duplicates, out-of-order delivery, recalls, every refusal, acknowledgements, alerts and poll backoff."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from opengrid.contracts.as_deployment_poll import (
    ALR_POLL_FAILED,
    ALR_POLL_STALE,
    ALR_REFUSED,
    R_AMBIGUOUS,
    R_LATE,
    R_NO_AWARD,
    CallOutcome,
    ErcotAsPoller,
    ErcotAsPollSettings,
    retry_delay_s,
    settings_from,
)
from opengrid.integrations.interfaces import (
    DispatchInstruction,
    InstructionBatch,
    MalformedInstruction,
    SubmissionReceipt,
)

T0 = datetime(2026, 9, 27, 5, 0, tzinfo=UTC)
CONTRACT = UUID("00000000-0000-7000-8000-000000000d03")
AWARD = uuid4()
MMS = {"endpoint": "http://sim/mms/ews/", "qse_code": "QOPENGRID", "user_id": "u", "signing": "none"}


def _settings(**kw: Any) -> ErcotAsPollSettings:
    return ErcotAsPollSettings.model_validate(
        {"enabled": True, "mms": MMS, "awards": {"OG_ESR_1:ECRS": str(CONTRACT)}, **kw}
    )


def _deploy(
    iid: str = "D1", *, at: datetime = T0, mw: str = "0.5", resource: str = "OG_ESR_1"
) -> DispatchInstruction:
    return DispatchInstruction(
        instruction_id=iid, kind="AS_DEPLOYMENT", resource_id=resource, service="ECRS", mw=Decimal(mw),
        start_at=at, end_at=at + timedelta(minutes=30), issued_at=at, ramp_minutes=10,
    )  # fmt: skip


def _recall(iid: str, of: str | None, *, at: datetime = T0) -> DispatchInstruction:
    return DispatchInstruction(
        instruction_id=iid, kind="AS_RECALL", resource_id="OG_ESR_1", service="ECRS", start_at=at,
        issued_at=at, recalls=of,
    )  # fmt: skip


@dataclass
class FakeSource:
    batches: list[InstructionBatch | Exception] = field(default_factory=list)
    acks: list[tuple[str, bool, str | None]] = field(default_factory=list)
    backend: str = "ercot_mms"

    async def fetch_instruction_batch(self, since: datetime) -> InstructionBatch:
        item = self.batches.pop(0) if self.batches else InstructionBatch()
        if isinstance(item, Exception):
            raise item
        return item

    async def acknowledge_instruction(
        self, instruction_id: str, *, accepted: bool, reason: str | None
    ) -> SubmissionReceipt:
        self.acks.append((instruction_id, accepted, reason))
        return SubmissionReceipt(submission_id=instruction_id, status="ACCEPTED", received_at=T0)

    async def close(self) -> None:
        return None


@dataclass
class FakeCalls:
    """Stands in for the shared core: idempotent per key, refuses what `refuse` says."""

    refuse: CallOutcome | None = None
    calls: dict[str, UUID] = field(default_factory=dict)
    deployed: list[tuple[str, UUID]] = field(default_factory=list)
    recalled: list[str] = field(default_factory=list)

    async def deploy(
        self, instruction: DispatchInstruction, *, obligation_id: UUID, principal: str, now: datetime
    ) -> CallOutcome:
        assert principal == "ercot:ercot_mms"
        if self.refuse is not None:
            return self.refuse
        if instruction.instruction_id in self.calls:
            return CallOutcome(
                accepted=True, call_id=str(self.calls[instruction.instruction_id]), duplicate=True
            )
        self.calls[instruction.instruction_id] = obligation_id
        self.deployed.append((instruction.instruction_id, obligation_id))
        return CallOutcome(accepted=True, call_id=f"call-{instruction.instruction_id}")

    async def recall(self, deployment_instruction_id: str, *, principal: str, now: datetime) -> CallOutcome:
        self.recalled.append(deployment_instruction_id)
        return CallOutcome(accepted=True, call_id=f"call-{deployment_instruction_id}")

    async def has_call(self, instruction_id: str, *, principal: str) -> bool:
        return instruction_id in self.calls


@dataclass
class FakeAwards:
    found: list[UUID] = field(default_factory=lambda: [AWARD])
    asked: list[tuple[UUID, datetime]] = field(default_factory=list)

    async def awards_covering(self, contract_id: UUID, at: datetime) -> list[UUID]:
        self.asked.append((contract_id, at))
        return list(self.found)


@dataclass
class FakeAlerts:
    open: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    raised: int = 0

    async def raise_once(self, rule: str, condition_key: str, summary: str, detail: dict[str, Any]) -> None:
        if (rule, condition_key) not in self.open:
            self.raised += 1
            self.open[(rule, condition_key)] = detail

    async def clear(self, rule: str, condition_key: str) -> None:
        self.open.pop((rule, condition_key), None)


@dataclass
class FakeTrace:
    rows: list[tuple[str, str, str, dict[str, Any]]] = field(default_factory=list)

    async def append(
        self, stream_id: str, decision_type: str, event_class: str, payload: dict[str, Any]
    ) -> None:
        self.rows.append((stream_id, decision_type, event_class, payload))

    def classes(self) -> list[str]:
        return [r[2] for r in self.rows]


def _poller(source: FakeSource | None = None, calls: FakeCalls | None = None, **kw: Any) -> ErcotAsPoller:
    return ErcotAsPoller(
        source=source or FakeSource(),
        calls=calls or FakeCalls(),
        awards=kw.pop("awards", FakeAwards()),
        alerts=kw.pop("alerts", FakeAlerts()),
        trace=kw.pop("trace", FakeTrace()),
        settings=_settings(**kw),
        started_at=T0,
    )


def _batch(*instructions: DispatchInstruction) -> InstructionBatch:
    return InstructionBatch(instructions=list(instructions))


# -- config ---------------------------------------------------------------------------------------------


class _Cfg:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


def test_it_is_off_by_default_and_needs_an_endpoint_when_on() -> None:
    assert settings_from(_Cfg({})).enabled is False
    with pytest.raises(ValidationError):
        ErcotAsPollSettings(enabled=True)
    with pytest.raises(ValidationError):
        _settings(stale_after_s=5.0, interval_s=5.0)


def test_the_shipped_config_keeps_it_off() -> None:
    import tomllib
    from pathlib import Path

    toml = Path(__file__).resolve().parents[3] / "config" / "orchestrator.toml"
    shipped = tomllib.loads(toml.read_text(encoding="utf-8"))["feeds"]["ercot_as_poll"]
    assert shipped["enabled"] is False
    assert "market_sim" not in tomllib.loads(toml.read_text(encoding="utf-8"))


def test_failed_polls_back_off_by_doubling_to_the_cap() -> None:
    settings = _settings(interval_s=5.0, retry_cap_s=60.0)
    assert [retry_delay_s(settings, n) for n in range(7)] == [5.0, 5.0, 10.0, 20.0, 40.0, 60.0, 60.0]


# -- deployments ----------------------------------------------------------------------------------------


async def test_a_deployment_goes_through_the_core_and_is_acknowledged_and_traced() -> None:
    source, calls, trace, awards = FakeSource([_batch(_deploy())]), FakeCalls(), FakeTrace(), FakeAwards()
    poller = _poller(source, calls, trace=trace, awards=awards)

    await poller.poll_if_due(now=T0 + timedelta(seconds=3))

    assert calls.deployed == [("D1", AWARD)]
    assert awards.asked == [(CONTRACT, T0)]
    assert source.acks == [("D1", True, None)]
    ((stream, decision, event, payload),) = trace.rows
    assert (stream, decision, event) == ("ercot_as_poll", "FEED_CHANGE", "AS_INSTRUCTION_ACCEPTED")
    assert payload["origin"] == "ERCOT_POLL" and payload["obligation_id"] == str(AWARD)
    assert payload["ramp_minutes"] == 10 and payload["mw"] == "0.5"


async def test_no_instruction_means_nothing_is_written() -> None:
    """Enabling the poller alone changes nothing for committed ERCOT_AS obligations (their 0 kW hold)."""
    calls, trace, alerts = FakeCalls(), FakeTrace(), FakeAlerts()
    poller = _poller(FakeSource([InstructionBatch()]), calls, trace=trace, alerts=alerts)
    await poller.poll_if_due(now=T0)
    assert calls.deployed == [] and trace.rows == [] and alerts.open == {}


async def test_a_duplicate_delivery_never_deploys_twice_and_is_acknowledged_again() -> None:
    source, calls = FakeSource([_batch(_deploy()), _batch(_deploy())]), FakeCalls()
    poller = _poller(source, calls)
    await poller.poll_once(now=T0)
    await poller.poll_once(now=T0 + timedelta(seconds=5))
    assert calls.deployed == [("D1", AWARD)]
    assert source.acks == [("D1", True, None), ("D1", True, None)]


async def test_a_duplicate_after_a_restart_is_recognised_by_the_core_key() -> None:
    calls = FakeCalls(calls={"D1": AWARD})
    source, trace = FakeSource([_batch(_deploy())]), FakeTrace()
    await _poller(source, calls, trace=trace).poll_once(now=T0)
    assert calls.deployed == [] and source.acks == [("D1", True, None)]
    assert trace.classes() == ["AS_INSTRUCTION_DUPLICATE"]


# -- recalls and ordering --------------------------------------------------------------------------------


async def test_a_recall_ends_its_deployment_through_the_core() -> None:
    source, calls = (
        FakeSource([_batch(_deploy()), _batch(_recall("R1", "D1", at=T0 + timedelta(minutes=2)))]),
        FakeCalls(),
    )
    poller = _poller(source, calls)
    await poller.poll_once(now=T0)
    await poller.poll_once(now=T0 + timedelta(minutes=2))
    assert calls.recalled == ["D1"]
    assert source.acks[-1] == ("R1", True, None)


async def test_a_batch_delivered_out_of_order_never_starts_the_recalled_deployment() -> None:
    later = T0 + timedelta(seconds=1)
    source, calls, trace = (
        FakeSource([_batch(_recall("R1", "D1", at=later), _deploy())]),
        FakeCalls(),
        FakeTrace(),
    )
    await _poller(source, calls, trace=trace).poll_once(now=T0 + timedelta(seconds=5))
    assert calls.deployed == [] and calls.recalled == []
    assert trace.classes() == ["AS_INSTRUCTION_SUPERSEDED", "AS_RECALL_APPLIED"]
    assert {a[0] for a in source.acks} == {"D1", "R1"}


async def test_a_recall_that_beats_its_deployment_is_held_until_the_deployment_arrives() -> None:
    recall = _recall("R1", "D1", at=T0 + timedelta(seconds=1))
    source, calls, trace = FakeSource([_batch(recall), _batch(recall, _deploy())]), FakeCalls(), FakeTrace()
    poller = _poller(source, calls, trace=trace)
    await poller.poll_once(now=T0 + timedelta(seconds=2))
    assert source.acks == []  # held: unacknowledged, so the source keeps re-sending it
    await poller.poll_once(now=T0 + timedelta(seconds=25))
    assert calls.deployed == []
    assert {a[0] for a in source.acks} == {"D1", "R1"} and all(a[1] for a in source.acks)


async def test_a_recall_with_nothing_to_recall_is_acknowledged_after_the_hold() -> None:
    recall = _recall("R1", "D9")
    source = FakeSource([_batch(recall), _batch(recall)])
    poller = _poller(source, recall_hold_s=60.0)
    await poller.poll_once(now=T0 + timedelta(seconds=10))
    await poller.poll_once(now=T0 + timedelta(seconds=61))
    assert source.acks == [("R1", True, None)]


async def test_a_plain_vdi_is_noted_and_acknowledged_but_never_deploys() -> None:
    vdi = DispatchInstruction(
        instruction_id="V1", kind="VDI", resource_id="OG_ESR_1", start_at=T0, issued_at=T0, text="hold"
    )
    source, calls, trace = FakeSource([_batch(vdi)]), FakeCalls(), FakeTrace()
    await _poller(source, calls, trace=trace).poll_once(now=T0)
    assert (
        calls.deployed == []
        and trace.classes() == ["AS_INSTRUCTION_NOTED"]
        and source.acks == [("V1", True, None)]
    )


# -- refusals ---------------------------------------------------------------------------------------------


async def _refused(
    poller: ErcotAsPoller, batch: InstructionBatch, source: FakeSource, alerts: FakeAlerts
) -> tuple[str, bool, str | None]:
    source.batches.append(batch)
    await poller.poll_once(now=T0 + timedelta(seconds=3))
    (ack,) = source.acks
    assert ack[1] is False
    assert any(rule == ALR_REFUSED for rule, _ in alerts.open)
    return ack


async def test_a_malformed_instruction_is_rejected_422_alone() -> None:
    source, alerts, calls = FakeSource(), FakeAlerts(), FakeCalls()
    poller = _poller(source, calls, alerts=alerts)
    batch = InstructionBatch(
        instructions=[], malformed=[MalformedInstruction(instruction_id="M1", error="bad mw")]
    )
    ack = await _refused(poller, batch, source, alerts)
    assert ack[0] == "M1" and ack[2] is not None and ack[2].startswith("422")
    assert calls.deployed == []


async def test_an_instruction_for_an_unknown_award_is_rejected_404() -> None:
    source, alerts = FakeSource(), FakeAlerts()
    poller = _poller(source, awards=FakeAwards(found=[]), alerts=alerts)
    ack = await _refused(poller, _batch(_deploy()), source, alerts)
    assert ack[2] is not None and ack[2].startswith(f"404 {R_NO_AWARD}")


async def test_an_unmapped_resource_is_an_unknown_award_without_a_database_read() -> None:
    source, alerts, awards = FakeSource(), FakeAlerts(), FakeAwards()
    poller = _poller(source, awards=awards, alerts=alerts)
    ack = await _refused(poller, _batch(_deploy(resource="OG_ESR_UNKNOWN")), source, alerts)
    assert ack[2] is not None and ack[2].startswith("404") and awards.asked == []


async def test_two_covering_awards_are_refused_409_not_guessed() -> None:
    source, alerts = FakeSource(), FakeAlerts()
    poller = _poller(source, awards=FakeAwards(found=[uuid4(), uuid4()]), alerts=alerts)
    ack = await _refused(poller, _batch(_deploy()), source, alerts)
    assert ack[2] is not None and ack[2].startswith(f"409 {R_AMBIGUOUS}")


@pytest.mark.parametrize(
    ("status", "code"),
    [(409, "R-CALL-EXCEEDS-AWARD"), (409, "R-CALL-NOT-DEPLOYABLE"), (409, "R-CALL-OVERLAP"), (404, "R-CALL-NO-OBLIGATION"),
     (422, "R-CALL-INVALID")],
)  # fmt: skip
async def test_every_core_refusal_is_rejected_with_its_status_and_code(status: int, code: str) -> None:
    source, alerts = FakeSource(), FakeAlerts()
    calls = FakeCalls(
        refuse=CallOutcome(accepted=False, reason_code=code, http_status=status, detail="core says no")
    )
    poller = _poller(source, calls, alerts=alerts)
    ack = await _refused(poller, _batch(_deploy(mw="1.5")), source, alerts)
    assert ack[2] == f"{status} {code}: core says no"
    (((_, key), detail),) = alerts.open.items()
    assert key == f"{ALR_REFUSED}:D1" and detail["http_status"] == status and detail["origin"] == "ERCOT_POLL"


async def test_a_late_instruction_is_refused_never_acted_on() -> None:
    source, alerts, calls = FakeSource(), FakeAlerts(), FakeCalls()
    poller = _poller(source, calls, alerts=alerts, max_instruction_age_s=300.0)
    source.batches.append(_batch(_deploy(at=T0 - timedelta(minutes=10))))
    await poller.poll_once(now=T0)
    assert calls.deployed == [] and source.acks[0][2] is not None and R_LATE in source.acks[0][2]


async def test_a_refused_duplicate_is_answered_the_same_without_a_second_alert() -> None:
    source, alerts = FakeSource([_batch(_deploy()), _batch(_deploy())]), FakeAlerts()
    poller = _poller(source, awards=FakeAwards(found=[]), alerts=alerts)
    await poller.poll_once(now=T0)
    await poller.poll_once(now=T0 + timedelta(seconds=5))
    assert alerts.raised == 1 and source.acks[0] == source.acks[1]


# -- poll health ------------------------------------------------------------------------------------------


async def test_poll_failures_back_off_alert_and_clear_on_recovery() -> None:
    boom = ConnectionError("sim down")
    source, alerts = FakeSource([boom, boom, boom, boom, boom, boom, InstructionBatch()]), FakeAlerts()
    poller = _poller(source, alerts=alerts, interval_s=5.0, stale_after_s=60.0, failure_alert_after=3)
    now = T0
    polled_at = []
    for _ in range(400):  # 5 s ticks, like og-feeds
        before = len(source.batches)
        await poller.poll_if_due(now=now)
        if len(source.batches) != before:
            polled_at.append((now - T0).total_seconds())
        if not source.batches:
            break
        now += timedelta(seconds=5)
    assert polled_at == [0, 5, 15, 35, 75, 135, 195]
    assert (ALR_POLL_FAILED, ALR_POLL_FAILED) not in alerts.open  # cleared by the good poll
    assert alerts.raised == 2  # FAILED at the 3rd failure, STALE once 60 s passed without a good poll


async def test_a_stale_alert_stays_open_while_polls_keep_failing() -> None:
    source, alerts = FakeSource([ConnectionError("x")] * 3), FakeAlerts()
    poller = _poller(source, alerts=alerts, stale_after_s=10.0)
    await poller.poll_if_due(now=T0 + timedelta(seconds=11))
    assert (ALR_POLL_STALE, ALR_POLL_STALE) in alerts.open


async def test_after_a_restart_a_redelivered_deployment_and_its_recall_end_the_real_call() -> None:
    calls = FakeCalls(calls={"D1": AWARD})  # applied before the restart, not yet acknowledged
    later = T0 + timedelta(minutes=1)
    source = FakeSource([_batch(_recall("R1", "D1", at=later), _deploy())])
    await _poller(source, calls).poll_once(now=later)
    assert calls.recalled == ["D1"] and calls.deployed == []
    assert {a[0] for a in source.acks} == {"D1", "R1"}


async def test_an_instruction_with_no_readable_id_alerts_once_across_polls() -> None:
    bad = InstructionBatch(malformed=[MalformedInstruction(instruction_id=None, error="no mRID")])
    source, alerts = FakeSource([bad, bad, bad]), FakeAlerts()
    poller = _poller(source, alerts=alerts)
    for n in range(3):
        await poller.poll_once(now=T0 + timedelta(seconds=5 * n))
    assert alerts.raised == 1 and source.acks == []
