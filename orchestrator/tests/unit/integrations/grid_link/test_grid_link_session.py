"""DNP3 outstation application layer (dnp3_session): READ of static points, SELECT-before-OPERATE and
DIRECT_OPERATE of CROBs and analog outputs, staging, echoes and control statuses. Pure bytes, no sockets."""

from __future__ import annotations

import struct

from opengrid.integrations.grid_link.config import Dnp3LinkSettings
from opengrid.integrations.grid_link.dnp3_session import OutstationSession
from opengrid.integrations.grid_link.model import (
    CallPhase,
    CancelCall,
    ControlVerdict,
    GridCommand,
    Heartbeat,
    L2Limit,
    LinkStatus,
    TollCall,
)
from opengrid.integrations.grid_link.points import GridLinkPointMap
from opengrid.integrations.scada_dnp3.codec import parse_response

from .fakes import FakeClock

SELECT, OPERATE, DIRECT, DIRECT_NR = 0x03, 0x04, 0x05, 0x06
LATCH_ON, LATCH_OFF, PULSE_ON = 0x03, 0x04, 0x01


def _crob(index: int, code: int) -> bytes:
    return bytes([12, 1, 0x17, 1, index, code, 1]) + struct.pack("<II", 0, 0) + b"\x00"


def _ao(index: int, value: int) -> bytes:
    return bytes([41, 1, 0x17, 1, index]) + struct.pack("<i", value) + b"\x00"


def _req(seq: int, function: int, body: bytes) -> bytes:
    return bytes([0xC0 | seq, function]) + body


def _statuses(response: bytes) -> list[int]:
    """Status byte of every echoed control object (qualifier 0x17 echoes)."""
    out: list[int] = []
    pos = 4
    while pos < len(response):
        group, variation, count = response[pos], response[pos + 1], response[pos + 3]
        size = 11 if group == 12 else {1: 5, 2: 3, 3: 5}[variation]
        pos += 4
        for _ in range(count):
            out.append(response[pos + size])  # after the 1-byte index prefix
            pos += 1 + size
    return out


class Recorder:
    def __init__(self, verdict: ControlVerdict = ControlVerdict.ACCEPTED) -> None:
        self.offered: list[GridCommand] = []
        self.validated: list[GridCommand] = []
        self.verdict = verdict

    def validate(self, command: GridCommand) -> ControlVerdict:
        self.validated.append(command)
        return self.verdict

    def offer(self, command: GridCommand) -> ControlVerdict:
        self.offered.append(command)
        return self.verdict


def _status() -> LinkStatus:
    return LinkStatus(
        available_kw=1234.5,
        delivered_kw=0.0,
        call_phase=CallPhase.IDLE,
        ems_call_id=0,
        call_reason=0,
        call_granted_kw=0.0,
        soc_pct=None,
        heartbeat_count=3,
        link_healthy=True,
        telemetry_stale=False,
        toll_calls_enabled=True,
    )


def _session(recorder: Recorder, clock: FakeClock, **dnp3: object) -> OutstationSession:
    return OutstationSession(
        GridLinkPointMap(["LZ_AEN"], ["bank-040"]),
        Dnp3LinkSettings(**dnp3),  # type: ignore[arg-type]
        validate=recorder.validate,
        offer=recorder.offer,
        status=_status,
        monotonic=clock,
        max_setpoint_kw=26000.0,
        max_duration_min=90,
    )


def test_class0_read_serves_static_binaries_and_float_analogs() -> None:
    session = _session(Recorder(), FakeClock())
    [response] = session.process(_req(1, 0x01, bytes([60, 1, 0x06])))
    parsed = parse_response(response)
    assert parsed.seq == 1 and parsed.fin and parsed.iin == 0
    analogs = {a.index: a for a in parsed.analogs}
    assert analogs[0].value == 1234.5 and analogs[7].value == 3.0
    assert analogs[6].value == -1.0  # SoC unknown -> sentinel
    assert analogs[100].flags & 0x04  # no bank telemetry -> COMM_LOST, never invented
    binaries = {b.index: b.value for b in parsed.binaries}
    assert binaries[0] is True and binaries[2] is False and binaries[6] is True


def test_unknown_object_and_qualifier_set_iin2_bits() -> None:
    session = _session(Recorder(), FakeClock())
    [response] = session.process(_req(2, 0x01, bytes([20, 1, 0x06])))
    assert parse_response(response).iin & 0x0200
    [response] = session.process(_req(3, 0x01, bytes([30, 5, 0x01, 0, 0])))
    assert parse_response(response).iin & 0x0400
    [response] = session.process(_req(4, 0x0D, b""))  # COLD_RESTART: not supported
    assert parse_response(response).iin & 0x0100


def test_staged_toll_call_executes_with_select_before_operate() -> None:
    recorder, clock = Recorder(), FakeClock()
    session = _session(recorder, clock)
    for seq, (index, value) in enumerate(((0, 1500), (1, 90), (2, 77))):
        [echo] = session.process(_req(seq, DIRECT, _ao(index, value)))
        assert _statuses(echo) == [0]
    [echo] = session.process(_req(5, SELECT, _crob(0, LATCH_ON)))
    assert _statuses(echo) == [0] and recorder.offered == []
    assert recorder.validated == [TollCall(ems_call_id=77, setpoint_kw=1500.0, duration_min=90)]
    [echo] = session.process(_req(6, OPERATE, _crob(0, LATCH_ON)))
    assert _statuses(echo) == [0]
    assert recorder.offered == [TollCall(ems_call_id=77, setpoint_kw=1500.0, duration_min=90)]
    # staging is consumed: a second execute has nothing to execute
    [echo] = session.process(_req(7, DIRECT, _crob(0, LATCH_ON)))
    assert _statuses(echo) == [3]  # FORMAT_ERROR


def test_operate_without_matching_select_is_no_select() -> None:
    recorder, clock = Recorder(), FakeClock()
    session = _session(recorder, clock)
    [echo] = session.process(_req(1, OPERATE, _crob(2, PULSE_ON)))
    assert _statuses(echo) == [2]
    session.process(_req(2, SELECT, _crob(2, PULSE_ON)))
    [echo] = session.process(_req(4, OPERATE, _crob(2, PULSE_ON)))  # sequence is not select + 1
    assert _statuses(echo) == [2]
    session.process(_req(5, SELECT, _crob(2, PULSE_ON)))
    clock.advance(11.0)  # past the select timeout
    [echo] = session.process(_req(6, OPERATE, _crob(2, PULSE_ON)))
    assert _statuses(echo) == [2] and recorder.offered == []


def test_stale_staging_is_refused_with_timeout() -> None:
    recorder, clock = Recorder(), FakeClock()
    session = _session(recorder, clock)
    for seq, (index, value) in enumerate(((0, 100), (1, 30), (2, 5))):
        session.process(_req(seq, DIRECT, _ao(index, value)))
    clock.advance(10.5)
    [echo] = session.process(_req(4, DIRECT, _crob(0, LATCH_ON)))
    assert _statuses(echo) == [1] and recorder.offered == []


def test_controls_outside_the_point_list_are_not_supported_and_ranges_enforced() -> None:
    recorder, clock = Recorder(), FakeClock()
    session = _session(recorder, clock)
    [echo] = session.process(_req(1, DIRECT, _crob(40, LATCH_ON) + _ao(9, 5)))
    assert _statuses(echo) == [4, 4] and recorder.offered == []
    [echo] = session.process(_req(2, DIRECT, _ao(0, 0) + _ao(1, 91)))
    assert _statuses(echo) == [12, 12]
    [echo] = session.process(_req(3, DIRECT, _crob(0, LATCH_OFF)))
    assert _statuses(echo) == [4]  # an "off" execute is not a thing


def test_l2_heartbeat_and_cancel_map_to_commands_and_verdicts_to_statuses() -> None:
    recorder, clock = Recorder(ControlVerdict.INHIBITED), FakeClock()
    session = _session(recorder, clock)
    [echo] = session.process(_req(1, DIRECT, _crob(2, PULSE_ON) + _crob(16, LATCH_ON) + _crob(1, PULSE_ON)))
    assert _statuses(echo) == [10, 10, 10]
    assert recorder.offered == [Heartbeat(), L2Limit("LZ_AEN", True), CancelCall(None)]


def test_sbo_only_mode_refuses_direct_execute_and_nr_gives_no_response() -> None:
    recorder, clock = Recorder(), FakeClock()
    session = _session(recorder, clock, control_mode="sbo_only")
    for seq, (index, value) in enumerate(((0, 100), (1, 30), (2, 5))):
        session.process(_req(seq, DIRECT, _ao(index, value)))
    [echo] = session.process(_req(4, DIRECT, _crob(0, LATCH_ON)))
    assert _statuses(echo) == [9] and recorder.offered == []
    assert session.process(_req(5, DIRECT_NR, _crob(2, PULSE_ON))) == []
    assert recorder.offered == [Heartbeat()]


def test_large_reads_are_split_into_confirmed_fragments() -> None:
    session = OutstationSession(
        GridLinkPointMap([], [f"bank-{i:03d}" for i in range(120)]),
        Dnp3LinkSettings(max_fragment_size=512),
        validate=Recorder().validate,
        offer=Recorder().offer,
        status=_status,
        monotonic=FakeClock(),
        max_setpoint_kw=1.0,
        max_duration_min=90,
    )
    responses = session.process(_req(1, 0x01, bytes([60, 1, 0x06])))
    assert len(responses) > 1
    parsed = [parse_response(r) for r in responses]
    assert parsed[0].fir and all(p.con and not p.fin for p in parsed[:-1]) and parsed[-1].fin
    assert [p.seq for p in parsed] == [(1 + n) & 0x0F for n in range(len(parsed))]
    assert all(len(r) <= 512 for r in responses)
    indices = {a.index for p in parsed for a in p.analogs}
    assert len(indices) == 8 + 120 * 4
