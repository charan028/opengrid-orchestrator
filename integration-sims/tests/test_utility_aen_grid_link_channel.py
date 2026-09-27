"""The Austin Energy sim's grid-link Channel on its own (a fake DNP3 master; no sockets, no opengrid):
call-id mapping, staging order and SBO, refusal reasons, state decoding, shorten refused, registry."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from ogsim.protocols import gridlink_points as gp
from ogsim.protocols.dnp3_master import Dnp3Master, Dnp3MasterError, PointValues
from ogsim.utility_aen.channels import CHANNEL_FACTORIES, build_channel
from ogsim.utility_aen.channels.base import CallSpec, ChannelError
from ogsim.utility_aen.channels.grid_link import GridLinkChannel, ems_call_id

CONTRACT = Path(__file__).resolve().parents[2] / "interfaces" / "grid_link" / "opengrid-gridlink-v1.json"
NOW = datetime(2026, 9, 27, 21, 30, tzinfo=UTC)


class FakeMaster:
    def __init__(self) -> None:
        self.connected = False
        self.ops: list[tuple[str, int, int, bool]] = []
        self.statuses: dict[tuple[str, int], int] = {}
        self.points = PointValues()
        self.fail = False

    async def connect(self) -> None:
        if self.fail:
            raise Dnp3MasterError("connect refused")
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def analog_output(self, index: int, value: int, *, sbo: bool = False) -> int:
        self.ops.append(("AO", index, value, sbo))
        return self.statuses.get(("AO", index), 0)

    async def crob(self, index: int, code: int, *, sbo: bool = False) -> int:
        self.ops.append(("CROB", index, code, sbo))
        return self.statuses.get(("CROB", index), 0)

    async def integrity_poll(self) -> PointValues:
        return self.points

    def report(self, call_id: int, state: int, reason: int = 0, delivered: float | None = None) -> None:
        self.points.analogs.update({gp.AI["CALL_ID"]: float(call_id), gp.AI["CALL_STATE"]: float(state)})
        self.points.analogs[gp.AI["CALL_REASON"]] = float(reason)
        if delivered is not None:
            self.points.analogs[gp.AI["CALL_DELIVERED_KW"]] = delivered
            self.points.analog_flags[gp.AI["CALL_DELIVERED_KW"]] = gp.FLAG_ONLINE
        else:
            self.points.analog_flags[gp.AI["CALL_DELIVERED_KW"]] = 0x04


def _channel(**settings: Any) -> tuple[GridLinkChannel, FakeMaster]:
    master = FakeMaster()
    channel = GridLinkChannel({"status_wait_s": 0.0, **settings}, master=cast(Dnp3Master, master))
    return channel, master


def _spec(ref: str = "4242", kw: float = -1500.0, minutes: int = 90) -> CallSpec:
    return CallSpec(call_ref=ref, kw=kw, start=NOW, duration_min=minutes)


def test_call_reason_codes_match_the_interfaces_contract() -> None:
    spec = json.loads(CONTRACT.read_text(encoding="utf-8"))["call_reason_codes"]
    assert {v: k for k, v in spec.items() if k != "NONE"} == gp.CALL_REASONS


def test_ems_call_id_is_the_number_or_a_stable_hash() -> None:
    assert ems_call_id("4242") == 4242
    hashed = ems_call_id("AEN-2026-09-27-001")
    assert hashed == ems_call_id("AEN-2026-09-27-001") and 0 < hashed <= 2**31 - 1
    assert ems_call_id(str(2**31)) != 2**31  # out of range: hashed, never truncated


def test_registered_in_the_channel_registry() -> None:
    assert "grid_link" in CHANNEL_FACTORIES
    assert isinstance(build_channel("grid_link", {"host": "127.0.0.1", "port": 20001}), GridLinkChannel)


async def test_issue_stages_then_executes_with_sbo_and_reports_the_state() -> None:
    channel, master = _channel()
    master.report(4242, 1)
    result = await channel.issue_call(_spec())
    assert result.accepted and result.state == "ACCEPTED" and result.delivered_kw is None
    assert master.ops[0] == ("CROB", gp.BO["HEARTBEAT"], gp.CROB_PULSE_ON, False)  # heartbeat before a call
    assert master.ops[1:] == [
        ("AO", 0, 1500, False),
        ("AO", 1, 90, False),
        ("AO", 2, 4242, False),
        ("CROB", 0, gp.CROB_LATCH_ON, True),
    ]
    master.report(4242, 2, delivered=1480.0)
    status = await channel.status("4242")
    assert status.state == "ACTIVE" and status.delivered_kw == -1480.0
    await channel.aclose()


async def test_orchestrator_refusal_is_returned_with_its_reason() -> None:
    channel, master = _channel()
    master.report(4242, 4, reason=9)
    result = await channel.issue_call(_spec())
    assert not result.accepted and result.state == "REFUSED" and result.reason_code == "R-CALL-OUTSIDE-WINDOW"
    await channel.aclose()


@pytest.mark.parametrize(
    ("kw", "minutes", "index", "reason"),
    [(100.0, 30, 0, "R-CALL-CHARGE-REFUSED"), (-100.0, 120, 1, "R-CALL-DURATION-CAP")],
)
async def test_outstation_range_refusals_map_to_call_reasons(
    kw: float, minutes: int, index: int, reason: str
) -> None:
    channel, master = _channel()
    master.statuses[("AO", index)] = gp.STATUS["OUT_OF_RANGE"]
    result = await channel.issue_call(_spec(kw=kw, minutes=minutes))
    assert result.state == "REFUSED" and result.reason_code == reason
    assert ("CROB", 0, gp.CROB_LATCH_ON, True) not in master.ops  # never executed
    await channel.aclose()


async def test_inhibited_execute_reports_link_down() -> None:
    channel, master = _channel()
    master.statuses[("CROB", gp.BO["CALL_EXECUTE"])] = gp.STATUS["AUTOMATION_INHIBIT"]
    result = await channel.issue_call(_spec())
    assert result.reason_code == "R-GL-LINK-DOWN"
    await channel.aclose()


async def test_cancel_now_and_shorten_refused_locally() -> None:
    channel, master = _channel(sbo=False)
    master.report(4242, 3)
    result = await channel.cancel("4242")
    assert result.state == "COMPLETED"
    assert master.ops[-2:] == [("AO", 2, 4242, False), ("CROB", 1, gp.CROB_PULSE_ON, False)]
    later = datetime.now(UTC) + timedelta(minutes=10)
    shorten = await channel.cancel("4242", end_at=later)
    assert shorten.reason_code == "R-GL-SHORTEN-UNSUPPORTED"
    await channel.aclose()


async def test_a_different_call_on_the_points_reads_unknown() -> None:
    channel, master = _channel()
    master.report(7, 2)
    assert (await channel.status("4242")).state == "UNKNOWN"
    await channel.aclose()


async def test_transport_failure_raises_channel_error() -> None:
    channel, master = _channel()
    master.fail = True
    with pytest.raises(ChannelError):
        await channel.issue_call(_spec())
