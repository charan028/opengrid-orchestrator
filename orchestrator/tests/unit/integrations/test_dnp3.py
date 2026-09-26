"""DNP3 master adapter end-to-end against the ogsim DNP3 outstation over real DNP3 on localhost TCP
(an OS-assigned port >= 18000; no broker, no other network)."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator
from typing import Any

import pytest

from opengrid.integrations.scada_dnp3.adapter import (
    Dnp3OutstationSettings,
    Dnp3ScadaSource,
    Dnp3Settings,
    dnp3_quality,
)
from opengrid.integrations.scada_dnp3.channel import Dnp3ChannelError, Dnp3MasterChannel
from opengrid.integrations.scada_dnp3.codec import (
    FLAG_COMM_LOST,
    FLAG_ONLINE,
    FLAG_OVER_RANGE,
    FLAG_RESTART,
    crc16_dnp,
)
from opengrid.integrations.scada_dnp3.points import AnalogPoint, Dnp3PointMap, standard_layout
from opengrid.integrations.sinks import CollectingSink

outstation_mod = pytest.importorskip("ogsim.protocols.dnp3_outstation")
BANKS = ["bank-000", "bank-001", "bank-002"]


@pytest.fixture
async def outstation() -> AsyncIterator[tuple[Any, int]]:
    sim = outstation_mod.Dnp3OutstationSim(BANKS, port=0)
    _, port = await sim.start()
    assert port >= 18000
    try:
        yield sim, port
    finally:
        await sim.stop()


def _source(port: int, **overrides: Any) -> Dnp3ScadaSource:
    settings = Dnp3Settings(
        outstations=[Dnp3OutstationSettings(name="sub-north", host="127.0.0.1", port=port, banks=BANKS)],
        response_timeout_s=2.0,
        **overrides,
    )
    return Dnp3ScadaSource(settings)


async def test_integrity_poll_maps_bank_analogs_with_scaling(outstation: tuple[Any, int]) -> None:
    sim, port = outstation
    sim.set_bank_kva("bank-000", 412.3)
    sim.set_signal("bank-001", "APPARENT_POWER_KVA", 598.7)
    sim.set_signal("bank-001", "VOLTAGE_A_PU", 1.0312)
    sim.set_signal("bank-001", "FREQUENCY_HZ", 59.987)
    source = _source(port)
    sink = CollectingSink()
    try:
        await source.poll_once(sink)
    finally:
        await source.close()
    kva = sink.latest("bank-000", "APPARENT_POWER_KVA")
    assert (
        kva is not None and kva.value == pytest.approx(412.3) and kva.unit == "kVA" and kva.quality == "good"
    )
    amps = sink.latest("bank-000", "CURRENT_A_PHASE_B")
    assert amps is not None and amps.value == pytest.approx(412.3e3 / (3**0.5 * 480), abs=0.05)
    assert sink.latest("bank-001", "VOLTAGE_A_PU").value == pytest.approx(1.0312)  # type: ignore[union-attr]
    assert sink.latest("bank-001", "FREQUENCY_HZ").value == pytest.approx(59.987)  # type: ignore[union-attr]
    # never-written points (RESTART flag) are withheld, not delivered as 0
    assert sink.latest("bank-002", "APPARENT_POWER_KVA") is None
    assert sink.latest("bank-001", "REAL_POWER_KW") is None
    assert sink.instructions == []


async def test_event_poll_carries_changes_and_instruction_levels(outstation: tuple[Any, int]) -> None:
    sim, port = outstation
    sim.set_bank_kva("bank-000", 300.0)
    source = _source(port)
    sink = CollectingSink()
    try:
        await source.poll_once(sink)
        sim.set_bank_kva("bank-000", 555.5)
        sim.set_instruction("bank-002", "LIMIT", limit_kw=250.0)
        await source.event_poll_once(sink)
        assert sink.latest("bank-000", "APPARENT_POWER_KVA").value == pytest.approx(555.5)  # type: ignore[union-attr]
        assert [(i.bank_id, i.kind, i.limit_kw, i.expires_at) for i in sink.instructions] == [
            ("bank-002", "LIMIT", 250.0, None)
        ]
        # unchanged levels: no new instruction
        await source.event_poll_once(sink)
        assert len(sink.instructions) == 1
        # ESTOP overrides the LIMIT, then clearing lifts it
        sim.set_instruction("bank-002", "ESTOP")
        await source.event_poll_once(sink)
        assert sink.instructions[-1].kind == "ESTOP"
        sim.set_instruction("bank-002", None)
        await source.event_poll_once(sink)
        lifted = sink.instructions[-1]
        assert lifted.kind == "ESTOP" and lifted.expires_at == lifted.issued_at
    finally:
        await source.close()


async def test_comm_lost_and_breaker_open(outstation: tuple[Any, int]) -> None:
    sim, port = outstation
    sim.set_bank_kva("bank-000", 100.0)
    sim.set_bank_kva("bank-001", 100.0)
    sim.set_status("bank-000", "COMM_OK", False)  # RTU lost the bank meter
    sim.set_status("bank-001", "BREAKER_CLOSED", False)
    source = _source(port)
    sink = CollectingSink()
    try:
        await source.poll_once(sink)
    finally:
        await source.close()
    assert sink.latest("bank-000", "APPARENT_POWER_KVA") is None  # withheld: comm_fail
    assert [(i.bank_id, i.kind) for i in sink.instructions] == [("bank-001", "BLOCK")]


async def test_deliver_bad_quality_opt_in(outstation: tuple[Any, int]) -> None:
    sim, port = outstation
    sim.set_bank_kva("bank-000", 100.0, quality="comm_fail")
    source = _source(port, deliver_bad_quality=True)
    sink = CollectingSink()
    try:
        await source.poll_once(sink)
    finally:
        await source.close()
    reading = sink.latest("bank-000", "APPARENT_POWER_KVA")
    assert reading is not None and reading.quality == "comm_fail"


async def test_scada_tick_feed_matches_mqtt_view(outstation: tuple[Any, int]) -> None:
    sim, port = outstation
    ts = "2026-09-26T18:00:00Z"
    signals = [
        ("scada/bank-000", {"bank_id": "bank-000", "signal": "APPARENT_POWER_KVA", "value": 480.25,
                            "unit": "kVA", "quality": "good", "ts": ts}),
        ("scada/bank-999", {"bank_id": "bank-999", "signal": "APPARENT_POWER_KVA", "value": 1.0,
                            "unit": "kVA", "quality": "good", "ts": ts}),
    ]  # fmt: skip
    instructions = [
        ("scada/instruction/bank-001", {"instruction_id": "x", "bank_id": "bank-001", "kind": "LIMIT",
                                        "limit_kw": 540.0, "issued_at": ts, "expires_at": None,
                                        "issued_by": "SCADA_SIM"}),
    ]  # fmt: skip
    assert sim.apply_scada_tick(signals, instructions) == 2
    source = _source(port)
    sink = CollectingSink()
    try:
        await source.poll_once(sink)
    finally:
        await source.close()
    assert sink.latest("bank-000", "APPARENT_POWER_KVA").value == pytest.approx(480.2, abs=0.06)  # type: ignore[union-attr]
    assert [(i.bank_id, i.kind, i.limit_kw) for i in sink.instructions] == [("bank-001", "LIMIT", 540.0)]


async def test_run_loop_reconnects_after_outstation_restart() -> None:
    sim = outstation_mod.Dnp3OutstationSim(BANKS, port=0)
    _, port = await sim.start()
    sim.set_bank_kva("bank-000", 111.0)
    settings = Dnp3Settings(
        outstations=[Dnp3OutstationSettings(name="s", host="127.0.0.1", port=port, banks=BANKS)],
        event_poll_s=0.05,
        response_timeout_s=0.5,
        connect_timeout_s=0.5,
        reconnect_min_s=0.05,
        reconnect_max_s=0.1,
    )
    source = Dnp3ScadaSource(settings)
    sink = CollectingSink()
    task = asyncio.create_task(source.run(sink))
    try:
        await _until(lambda: sink.latest("bank-000", "APPARENT_POWER_KVA") is not None)
        await sim.stop()
        await asyncio.sleep(0.3)
        count_while_down = len(sink.signals)
        await asyncio.sleep(0.3)
        assert len(sink.signals) == count_while_down  # nothing delivered while the outstation is down
        sim2 = outstation_mod.Dnp3OutstationSim(BANKS, port=port)
        await sim2.start()
        sim2.set_bank_kva("bank-000", 222.0)
        try:
            await _until(lambda: sink.latest("bank-000", "APPARENT_POWER_KVA").value == pytest.approx(222.0))  # type: ignore[union-attr]
        finally:
            await sim2.stop()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await source.close()


async def _until(predicate: Any, timeout_s: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


async def test_channel_timeout_and_refused() -> None:
    held: list[asyncio.StreamWriter] = []

    async def silent(_r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        held.append(w)  # accept and never answer

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    channel = Dnp3MasterChannel(
        "127.0.0.1", port, master_address=1, outstation_address=10, response_timeout_s=0.2
    )
    try:
        await channel.connect()
        with pytest.raises(Dnp3ChannelError, match="timeout"):
            await channel.request(bytes([0xC0, 0x01, 0x3C, 0x01, 0x06]))
    finally:
        await channel.close()
        for w in held:
            w.close()
        server.close()
        await server.wait_closed()
    refused = Dnp3MasterChannel(
        "127.0.0.1", port, master_address=1, outstation_address=10, connect_timeout_s=0.5
    )
    with pytest.raises(Dnp3ChannelError, match="connect"):
        await refused.connect()


def test_quality_mapping() -> None:
    assert dnp3_quality(FLAG_ONLINE) == "good"
    assert dnp3_quality(FLAG_RESTART) == "missing"
    assert dnp3_quality(FLAG_ONLINE | FLAG_COMM_LOST) == "comm_fail"
    assert dnp3_quality(0) == "comm_fail"
    assert dnp3_quality(FLAG_ONLINE | FLAG_OVER_RANGE) == "out_of_range"


def test_standard_layout_and_duplicate_indices() -> None:
    layout = standard_layout(["a", "b"])
    assert len(layout.analogs) == 28 and len(layout.binaries) == 10
    assert {p.index for p in layout.analogs if p.bank_id == "b"} == set(range(16, 30))
    assert layout.bank_ids() == ["a", "b"]
    with pytest.raises(ValueError, match="duplicate"):
        Dnp3PointMap(
            analogs=[
                AnalogPoint(index=1, bank_id="a", role="APPARENT_POWER_KVA"),
                AnalogPoint(index=1, bank_id="a", role="REAL_POWER_KW"),
            ]
        )


def test_dnp3_crc_matches_the_ieee_1815_reference_frame() -> None:
    # Reset Link States header from IEEE 1815 Annex examples: 05 64 05 C0 01 00 00 04 -> CRC E9 21
    header = bytes([0x05, 0x64, 0x05, 0xC0, 0x01, 0x00, 0x00, 0x04])
    assert crc16_dnp(header).to_bytes(2, "little") == bytes([0xE9, 0x21])


def test_codec_parses_packed_prefixed_and_timed_objects() -> None:
    from opengrid.integrations.scada_dnp3.codec import parse_response

    fragment = (
        bytes([0xC0, 0x81, 0x00, 0x00])
        + bytes([1, 1, 0x00, 0, 9]) + bytes([0b00000101, 0b10])  # g1v1 packed, indices 0..9
        + bytes([51, 1, 0x07, 1]) + bytes(6)  # CTO, skipped
        + bytes([32, 3, 0x17, 1, 7, 0x01]) + (1234).to_bytes(4, "little", signed=True) + bytes(6)  # g32v3
        + bytes([30, 5, 0x01, 2, 0, 2, 0, 0x21]) + struct.pack("<f", 1.5)  # g30v5 float, over-range
    )  # fmt: skip
    parsed = parse_response(fragment)
    states = {b.index: b.value for b in parsed.binaries}
    assert states[0] and not states[1] and states[2] and states[9] and len(states) == 10
    assert [(a.index, a.value, a.flags) for a in parsed.analogs] == [(7, 1234.0, 0x01), (2, 1.5, 0x21)]


def test_codec_rejects_unknown_objects_and_bad_frames() -> None:
    from opengrid.integrations.scada_dnp3.codec import (
        Dnp3ParseError,
        LinkParser,
        Reassembler,
        build_link_frame,
        parse_response,
        segment_fragment,
    )

    with pytest.raises(Dnp3ParseError, match="unsupported object g20v1"):
        parse_response(bytes([0xC0, 0x81, 0, 0, 20, 1, 0x00, 0, 0]) + bytes(5))
    with pytest.raises(Dnp3ParseError, match="not a response"):
        parse_response(bytes([0xC0, 0x01, 0, 0]))
    frame = bytearray(build_link_frame(0x44, 1, 10, b"hello"))
    frame[12] ^= 0x01  # corrupt a data byte: its block CRC no longer matches
    assert LinkParser().feed(bytes(frame)) == []
    segments, _ = segment_fragment(bytes(600), 62)
    reassembler = Reassembler()
    assert reassembler.add(segments[0]) is None
    assert reassembler.add(segments[2]) is None  # gap: partial fragment dropped
    assert reassembler.add(segments[1]) is None
