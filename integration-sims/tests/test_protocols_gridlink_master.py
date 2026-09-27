"""ogsim grid-link EMS master (`ogsim.protocols.dnp3_master`) and its point constants, on their own: the
constants equal the interfaces contract, objects decode, and SELECT/OPERATE and polls work against a tiny
in-test outstation on localhost. No opengrid import."""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ogsim.protocols import dnp3_wire as w
from ogsim.protocols import gridlink_points as gp
from ogsim.protocols.dnp3_master import Dnp3Master, Dnp3MasterError, PointValues, parse_objects

CONTRACT = Path(__file__).resolve().parents[2] / "interfaces" / "grid_link" / "opengrid-gridlink-v1.json"


def test_points_match_the_interfaces_contract() -> None:
    spec = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert (
        spec["analog_outputs"]["fixed"] == gp.AO
        and spec["analog_outputs"]["per_target"]["base"] == gp.AO_TARGET_BASE
    )
    assert (
        spec["binary_outputs"]["fixed"] == gp.BO
        and spec["binary_outputs"]["per_target"]["base"] == gp.BO_TARGET_BASE
    )
    assert spec["analog_inputs"]["fixed"] == gp.AI
    assert spec["analog_inputs"]["per_target"]["base"] == gp.AI_TARGET_BASE
    assert spec["analog_inputs"]["per_bank"]["base"] == gp.AI_BANK_BASE
    assert spec["analog_inputs"]["per_bank"]["stride"] == gp.AI_BANK_STRIDE
    assert (
        spec["binary_inputs"]["fixed"] == gp.BI
        and spec["binary_inputs"]["per_target"]["base"] == gp.BI_TARGET_BASE
    )
    assert {v: k for k, v in spec["call_state_codes"].items()} == gp.CALL_STATES
    assert {k: v for k, v in spec["control_status_codes"].items()} == gp.STATUS
    assert gp.CROB_PULSE_ON in spec["crob_codes"]["on"] and gp.CROB_LATCH_ON in spec["crob_codes"]["on"]
    assert gp.CROB_LATCH_OFF in spec["crob_codes"]["off"]


def test_parse_objects_decodes_inputs_and_control_echoes() -> None:
    fragment = (
        bytes([0xC1, 0x81, 0x00, 0x00])
        + bytes([1, 2, 0x28])
        + struct.pack("<H", 2)
        + struct.pack("<HB", 0, 0x81)
        + struct.pack("<HB", 6, 0x01)
        + bytes([30, 5, 0x28])
        + struct.pack("<H", 1)
        + struct.pack("<HB", 2, 0x01)
        + struct.pack("<f", 2.0)
        + bytes([30, 1, 0x01])
        + struct.pack("<HH", 7, 7)
        + bytes([0x01])
        + struct.pack("<i", 12)
        + bytes([12, 1, 0x17, 1, 0, 3, 1])
        + bytes(8)
        + bytes([10])
    )
    values, statuses = PointValues(), []
    parse_objects(fragment, values, statuses)
    assert values.binaries == {0: True, 6: False}
    assert values.analogs == {2: 2.0, 7: 12.0}
    assert statuses == [10]
    with pytest.raises(Dnp3MasterError):
        parse_objects(bytes([0xC1, 0x81, 0, 0, 20, 1, 0x06]), PointValues(), [])


class TinyOutstation:
    """Echoes controls with status 0 (or the configured status) and answers READ with one analog."""

    def __init__(self) -> None:
        self.requests: list[bytes] = []
        self.status = 0
        self._server: asyncio.Server | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return int(self._server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def _respond(self, request: bytes) -> bytes:
        seq, function = request[0] & 0x0F, request[1]
        head = bytes([0xC0 | seq, 0x81, 0, 0])
        if function == 0x01:
            return head + bytes([30, 5, 0x28]) + struct.pack("<HHB", 1, 0, 0x01) + struct.pack("<f", 1500.0)
        return head + request[2:-1] + bytes([self.status])

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        parser, reassembler, tseq = w.LinkParser(), w.Reassembler(), 0
        while data := await reader.read(4096):
            for frame in parser.feed(data):
                request = reassembler.add(frame.data)
                if request is None:
                    continue
                self.requests.append(request)
                segments, tseq = w.segment(self._respond(request), tseq)
                for seg in segments:
                    writer.write(w.build_link_frame(w.PRM | w.PRI_UNCONFIRMED_DATA, 1, 10, seg))
                await writer.drain()
        writer.close()


@pytest.fixture
async def outstation() -> AsyncIterator[tuple[TinyOutstation, int]]:
    sim = TinyOutstation()
    port = await sim.start()
    try:
        yield sim, port
    finally:
        await sim.stop()


async def test_select_before_operate_and_poll_against_an_outstation(
    outstation: tuple[TinyOutstation, int],
) -> None:
    sim, port = outstation
    master = Dnp3Master("127.0.0.1", port, timeout_s=2.0)
    await master.connect()
    try:
        assert await master.crob(gp.BO["CALL_EXECUTE"], gp.CROB_LATCH_ON, sbo=True) == 0
        assert [r[1] for r in sim.requests] == [0x03, 0x04]  # SELECT then OPERATE
        assert (sim.requests[1][0] & 0x0F) == ((sim.requests[0][0] & 0x0F) + 1) & 0x0F
        assert await master.analog_output(gp.AO["CALL_SETPOINT_KW"], 1500) == 0
        assert sim.requests[-1][1] == 0x05 and struct.unpack("<i", sim.requests[-1][7:11])[0] == 1500
        assert (await master.integrity_poll()).analogs == {0: 1500.0}
        sim.status = 10
        assert await master.crob(gp.BO["CALL_EXECUTE"], gp.CROB_LATCH_ON, sbo=True) == 10  # stops at SELECT
        assert sim.requests[-1][1] == 0x03
    finally:
        await master.close()


async def test_connect_failure_raises() -> None:
    master = Dnp3Master("127.0.0.1", 1, timeout_s=0.5)
    with pytest.raises(Dnp3MasterError):
        await master.connect()
