"""ogsim DNP3 outstation: point layout, scaling, event classes, the application responses and the TCP
link, checked byte-for-byte against IEEE 1815 encodings (no opengrid import, no DNP3 library)."""

from __future__ import annotations

import asyncio
import struct

from ogsim.protocols import dnp3_wire as w
from ogsim.protocols.dnp3_outstation import _SIGNAL_OFFSET, ANALOG_STRIDE, BINARY_STRIDE, Dnp3OutstationSim

INTEGRITY = bytes([0xC3, 0x01, 60, 2, 0x06, 60, 3, 0x06, 60, 4, 0x06, 60, 1, 0x06])
EVENTS = bytes([0xC4, 0x01, 60, 2, 0x06, 60, 3, 0x06, 60, 4, 0x06])


def _objects(fragment: bytes) -> list[tuple[int, int, int, bytes]]:
    """(group, variation, qualifier, body) for the object types this outstation emits."""
    out = []
    pos = 4
    while pos < len(fragment):
        group, variation, qualifier = fragment[pos : pos + 3]
        pos += 3
        if qualifier == 0x01:
            start, stop = struct.unpack("<HH", fragment[pos : pos + 4])
            size = 1 if group == 1 else 5
            end = pos + 4 + (stop - start + 1) * size
        else:  # 0x28
            (count,) = struct.unpack("<H", fragment[pos : pos + 2])
            size = 2 + (1 if group == 2 else 5)
            end = pos + 2 + count * size
        out.append((group, variation, qualifier, fragment[pos:end]))
        pos = end
    return out


def test_crc_matches_the_ieee_1815_reference_frame() -> None:
    assert w.crc16_dnp(bytes([0x05, 0x64, 0x05, 0xC0, 0x01, 0x00, 0x00, 0x04])).to_bytes(
        2, "little"
    ) == bytes([0xE9, 0x21])


def test_integrity_poll_serves_scaled_static_points() -> None:
    sim = Dnp3OutstationSim(["bank-000", "bank-001"])
    sim.set_bank_kva("bank-001", 432.1)
    sim.set_signal("bank-000", "FREQUENCY_HZ", 60.012)
    [fragment] = sim.process_request(INTEGRITY)
    assert fragment[0] & 0xC0 == 0xC0 and fragment[0] & 0x0F == 3 and fragment[1] == 0x81
    objects = _objects(fragment)
    static_ai = next(body for g, v, q, body in objects if (g, v, q) == (30, 1, 0x01))
    start, stop = struct.unpack("<HH", static_ai[:4])
    assert (start, stop) == (0, 13)  # 14 analogs per bank; banks are 16 apart, so one run per bank
    runs = [body for g, v, q, body in objects if (g, v, q) == (30, 1, 0x01)]
    assert len(runs) == 2
    assert struct.unpack("<HH", runs[1][:4]) == (ANALOG_STRIDE, ANALOG_STRIDE + 13)
    hz_flags, hz_counts = (
        runs[0][4 + 10 * 5],
        struct.unpack("<i", runs[0][4 + 10 * 5 + 1 : 4 + 10 * 5 + 5])[0],
    )
    assert (hz_flags, hz_counts) == (0x01, 60012)
    kva_counts = struct.unpack("<i", runs[1][4 + 1 : 4 + 5])[0]
    assert kva_counts == 4321  # 0.1 kVA per count
    bi = next(body for g, v, q, body in objects if (g, v, q) == (1, 2, 0x01))
    assert bi[4] == 0x81  # COMM_OK: online, state on


def test_changes_are_class_events_and_are_consumed() -> None:
    sim = Dnp3OutstationSim(["bank-000"])
    sim.process_request(INTEGRITY)
    sim.set_bank_kva("bank-000", 100.0, quality="good")
    sim.set_instruction("bank-000", "LIMIT", limit_kw=250.0)
    [fragment] = sim.process_request(EVENTS)
    iin = struct.unpack("<H", fragment[2:4])[0]
    assert iin & 0x000E == 0  # all events delivered in this response
    objects = _objects(fragment)
    bi_events = next(body for g, v, q, body in objects if (g, v) == (2, 1))
    ai_events = next(body for g, v, q, body in objects if (g, v) == (32, 1))
    assert struct.unpack("<H", bi_events[:2])[0] == 1  # LIMIT_ACTIVE (Class 1)
    assert struct.unpack("<HBi", bi_events[2:5] + b"\0\0\0\0")[:2] == (4, 0x81)
    indexed = {
        struct.unpack("<H", ai_events[2 + n * 7 : 4 + n * 7])[0]: struct.unpack(
            "<i", ai_events[5 + n * 7 : 9 + n * 7]
        )[0]
        for n in range(struct.unpack("<H", ai_events[:2])[0])
    }
    assert indexed[0] == 1000 and indexed[13] == 2500
    [again] = sim.process_request(EVENTS)
    assert _objects(again) == []


def test_unsupported_function_and_object() -> None:
    sim = Dnp3OutstationSim(["bank-000"])
    [reply] = sim.process_request(bytes([0xC0, 0x05]))  # DIRECT_OPERATE
    assert struct.unpack("<H", reply[2:4])[0] & w.IIN_NO_FUNC
    [reply] = sim.process_request(bytes([0xC1, 0x01, 20, 0, 0x06]))  # counters: not served
    assert struct.unpack("<H", reply[2:4])[0] & w.IIN_OBJECT_UNKNOWN
    assert sim.process_request(bytes([0xC2, 0x00])) == []  # CONFIRM


def test_apply_scada_tick_uses_the_mqtt_message_shape() -> None:
    sim = Dnp3OutstationSim(["bank-000", "bank-001"], derive_electricals=False)
    ts = "2026-09-26T18:00:00Z"
    applied = sim.apply_scada_tick(
        [
            ("scada/bank-000", {"bank_id": "bank-000", "signal": "APPARENT_POWER_KVA", "value": 50.0,
                                "unit": "kVA", "quality": "good", "ts": ts}),
            ("scada/bank-777", {"bank_id": "bank-777", "signal": "APPARENT_POWER_KVA", "value": 1.0,
                                "unit": "kVA", "quality": "good", "ts": ts}),
        ],
        [
            ("scada/instruction/bank-001", {"bank_id": "bank-001", "kind": "ESTOP", "limit_kw": None,
                                            "issued_at": ts, "expires_at": None}),
        ],
    )  # fmt: skip
    assert applied == 2
    assert sim.analogs[0].value == 500
    assert sim.binaries[BINARY_STRIDE + 3].value is True
    sim.apply_scada_tick(
        [], [("x", {"bank_id": "bank-001", "kind": "ESTOP", "issued_at": ts, "expires_at": ts})]
    )
    assert sim.binaries[BINARY_STRIDE + 3].value is False


def test_apply_scada_tick_preserves_the_sign_of_real_power_kw() -> None:
    """R3.4 fix: `set_bank_kva`'s `derive_electricals` guess used to silently overwrite the
    REAL_POWER_KW point with an unsigned `kva * power_factor` value -- losing the +import/-export
    sign the real SCADA signal carries (interfaces/mqtt/scada_bank_signal.schema.json's `value`
    description). The tick's own REAL_POWER_KW message (which always follows APPARENT_POWER_KVA for
    the same bank in one tick's signal list, `ogsim.scada.runtime.ScadaEngine.tick`) must be the one
    that lands on the point, signed, with `derive_electricals` still deriving everything else."""
    sim = Dnp3OutstationSim(["bank-000"])  # derive_electricals=True (default)
    ts = "2026-09-26T18:00:00Z"
    sim.apply_scada_tick(
        [
            ("scada/bank-000", {"bank_id": "bank-000", "signal": "APPARENT_POWER_KVA", "value": 300.0,
                                "unit": "kVA", "quality": "good", "ts": ts}),
            ("scada/bank-000", {"bank_id": "bank-000", "signal": "REAL_POWER_KW", "value": -294.0,
                                "unit": "kW", "quality": "good", "ts": ts}),
        ],
        [],
    )  # fmt: skip
    index = ANALOG_STRIDE * 0 + _SIGNAL_OFFSET["REAL_POWER_KW"]
    counts = sim.analogs[index].value
    assert counts < 0  # export, negative -- not silently overwritten by the unsigned kva guess
    assert counts == round(-294.0 / 0.1)  # 0.1 kW per count (_ANALOG_LAYOUT)
    # Everything else derive_electricals computes is still there (unaffected by this fix).
    voltage_index = ANALOG_STRIDE * 0 + _SIGNAL_OFFSET["VOLTAGE_PU"]
    assert sim.analogs[voltage_index].value == 10000  # 1.0 pu at 0.0001 pu/count


def test_large_databases_split_into_confirmable_fragments() -> None:
    sim = Dnp3OutstationSim([f"bank-{i:03d}" for i in range(40)])
    fragments = sim.process_request(INTEGRITY)
    assert len(fragments) > 1
    assert fragments[0][0] & 0x80 and not fragments[0][0] & 0x40 and fragments[0][0] & 0x20  # FIR, CON
    assert fragments[-1][0] & 0x40 and not fragments[-1][0] & 0x20  # FIN, no CON
    assert all(len(f) <= 2048 for f in fragments)


def test_link_frames_and_transport_roundtrip() -> None:
    payload = bytes(range(256)) * 3
    segments, seq = w.segment(payload, 5)
    assert len(segments) == 4 and seq == 9
    reassembler = w.Reassembler()
    parser = w.LinkParser()
    out = None
    for seg in segments:
        [frame] = parser.feed(b"\xff" + w.build_link_frame(0x44, 1, 10, seg))  # leading garbage resyncs
        out = reassembler.add(frame.data) or out
    assert out == payload
    bad = bytearray(w.build_link_frame(0x44, 1, 10, b"abc"))
    bad[-1] ^= 0xFF  # corrupt the data CRC
    assert w.LinkParser().feed(bytes(bad)) == []


async def test_tcp_link_status_and_read() -> None:
    sim = Dnp3OutstationSim(["bank-000"], port=0)
    _, port = await sim.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(w.build_link_frame(w.DIR | w.PRM | w.PRI_LINK_STATUS_REQ, 10, 1))
        [status_seg], _ = w.segment(INTEGRITY, 0)
        writer.write(w.build_link_frame(w.DIR | w.PRM | w.PRI_UNCONFIRMED_DATA, 10, 1, status_seg))
        await writer.drain()
        parser, reassembler, fragment, status = w.LinkParser(), w.Reassembler(), None, None
        while fragment is None:
            for frame in parser.feed(await asyncio.wait_for(reader.read(4096), 2.0)):
                if frame.control & 0x0F == w.SEC_LINK_STATUS and not frame.control & w.PRM:
                    status = frame
                elif frame.control & w.PRM:
                    fragment = reassembler.add(frame.data) or fragment
        assert status is not None and (status.destination, status.source) == (1, 10)
        assert fragment[1] == 0x81
        writer.close()
        await writer.wait_closed()
    finally:
        await sim.stop()
