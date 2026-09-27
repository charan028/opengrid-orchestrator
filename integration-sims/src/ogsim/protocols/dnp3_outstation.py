"""DNP3 outstation simulator serving the ogsim SCADA bank points (protocol-adapters.md S3.2).

Point layout "opengrid-dnp3-v1" (the one OpenGrid proposes to utilities), per bank position b:

    AI 16*b + 0..13: kVA, kW, V pu, I, Va/Vb/Vc pu, Ia/Ib/Ic, Hz, THD V %, THD I %, utility limit kW
    BI  8*b + 0..4 : COMM_OK, BREAKER_CLOSED, UTILITY_BLOCK, UTILITY_ESTOP, UTILITY_LIMIT_ACTIVE

Analogs travel as scaled 32-bit integers (g30v1 static, g32v1 events); the scale per offset is in
`_ANALOG_LAYOUT`. Analogs are Class 2 events (deadband one count); utility-instruction binaries are
Class 1; COMM_OK / BREAKER_CLOSED are Class 3. The wire codec is ogsim's own (`dnp3_wire`).

Feeding: `apply_scada_tick` takes exactly the `(topic_suffix, message)` pairs `ogsim.scada.ScadaEngine.tick`
publishes on MQTT, so the DNP3 view and the MQTT view of a bank are the same numbers. Electrical
quantities the MQTT sim does not publish (kW, V, I per phase, Hz, THD) are derived from kVA with a
fixed power factor and nominal voltage unless set explicitly with `set_signal`.

Simplifications (a simulator, not a certified outstation): events are removed when sent (no
confirm-before-clear), no unsolicited responses, no time sync, no controls.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import struct
from dataclasses import dataclass
from typing import Any

from ogsim.protocols import dnp3_wire as w

__all__ = ["ANALOG_STRIDE", "BINARY_STRIDE", "Dnp3OutstationSim", "Point"]

ANALOG_STRIDE = 16
BINARY_STRIDE = 8

# offset -> (signal name, engineering units per count)
_ANALOG_LAYOUT: dict[int, tuple[str, float]] = {
    0: ("APPARENT_POWER_KVA", 0.1),
    1: ("REAL_POWER_KW", 0.1),
    2: ("VOLTAGE_PU", 0.0001),
    3: ("CURRENT_A", 0.1),
    4: ("VOLTAGE_A_PU", 0.0001),
    5: ("VOLTAGE_B_PU", 0.0001),
    6: ("VOLTAGE_C_PU", 0.0001),
    7: ("CURRENT_A_PHASE_A", 0.1),
    8: ("CURRENT_A_PHASE_B", 0.1),
    9: ("CURRENT_A_PHASE_C", 0.1),
    10: ("FREQUENCY_HZ", 0.001),
    11: ("THD_V_PCT", 0.01),
    12: ("THD_I_PCT", 0.01),
    13: ("UTILITY_LIMIT_KW", 0.1),
}
_SIGNAL_OFFSET = {name: off for off, (name, _) in _ANALOG_LAYOUT.items()}
_BINARY_LAYOUT = ("COMM_OK", "BREAKER_CLOSED", "UTILITY_BLOCK", "UTILITY_ESTOP", "UTILITY_LIMIT_ACTIVE")
_BINARY_OFFSET = {name: off for off, name in enumerate(_BINARY_LAYOUT)}

# point flags
ONLINE, RESTART, COMM_LOST, OVER_RANGE, STATE = 0x01, 0x02, 0x04, 0x20, 0x80
_QUALITY_FLAGS = {
    "good": ONLINE,
    "stale": ONLINE,
    "missing": RESTART,
    "out_of_range": ONLINE | OVER_RANGE,
    "comm_fail": COMM_LOST,
}

_POWER_FACTOR = 0.98
_NOMINAL_LL_VOLTAGE_V = 480.0
_NOMINAL_HZ = 60.0
_MAX_FRAGMENT = 2048


@dataclass
class Point:
    value: int | bool
    flags: int
    event_class: int


class Dnp3OutstationSim:
    """A DNP3 outstation holding the bank points of `bank_ids` (in layout order)."""

    def __init__(
        self,
        bank_ids: list[str],
        *,
        address: int = 10,
        master_address: int = 1,
        host: str = "127.0.0.1",
        port: int = 20000,
        derive_electricals: bool = True,
    ) -> None:
        self.bank_ids = list(bank_ids)
        self._position = {bank_id: i for i, bank_id in enumerate(self.bank_ids)}
        self.address = address
        self.master_address = master_address
        self.host = host
        self.port = port
        self.derive_electricals = derive_electricals
        self.analogs: dict[int, Point] = {}
        self.binaries: dict[int, Point] = {}
        self.events: dict[int, list[tuple[str, int, int | bool, int]]] = {1: [], 2: [], 3: []}
        for b, bank_id in enumerate(self.bank_ids):
            for off in _ANALOG_LAYOUT:
                self.analogs[ANALOG_STRIDE * b + off] = Point(0, RESTART, 2)
            for off, name in enumerate(_BINARY_LAYOUT):
                event_class = 3 if name in ("COMM_OK", "BREAKER_CLOSED") else 1
                self.binaries[BINARY_STRIDE * b + off] = Point(False, RESTART, event_class)
            self.set_status(bank_id, "COMM_OK", True)
            self.set_status(bank_id, "BREAKER_CLOSED", True)
            for name in ("UTILITY_BLOCK", "UTILITY_ESTOP", "UTILITY_LIMIT_ACTIVE"):
                self.set_status(bank_id, name, False)
        for queue in self.events.values():
            queue.clear()
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[Any]] = set()

    # -- point writes --------------------------------------------------------------------------------

    def set_signal(self, bank_id: str, signal: str, value: float, *, quality: str = "good") -> None:
        """Write one engineering value (scaled to counts on the wire)."""
        off = _SIGNAL_OFFSET[signal]
        index = ANALOG_STRIDE * self._position[bank_id] + off
        counts = round(value / _ANALOG_LAYOUT[off][1])
        point = self.analogs[index]
        flags = _QUALITY_FLAGS[quality]
        if abs(counts - int(point.value)) >= 1 or flags != point.flags:
            self.events[point.event_class].append((w.AI_EVENT, index, counts, flags))
        point.value, point.flags = counts, flags

    def set_status(self, bank_id: str, name: str, value: bool, *, online: bool = True) -> None:
        index = BINARY_STRIDE * self._position[bank_id] + _BINARY_OFFSET[name]
        point = self.binaries[index]
        flags = ONLINE if online else COMM_LOST
        if bool(point.value) != value or flags != point.flags:
            self.events[point.event_class].append((w.BI_EVENT, index, value, flags))
        point.value, point.flags = value, flags

    def set_bank_kva(self, bank_id: str, kva: float, *, quality: str = "good") -> None:
        """Bank apparent power, plus derived V/I/Hz/THD when `derive_electricals`.

        R3.4 fix: REAL_POWER_KW is deliberately NOT derived here (it used to be guessed as
        `kva * _POWER_FACTOR`, always positive, since `kva` itself is an unsigned magnitude --
        `aggregation.kw_to_kva` is `abs(real_power_kw) / power_factor`). That threw away the sign the
        real REAL_POWER_KW signal carries (+ = import from the feeder, - = export -- pinned in
        `interfaces/mqtt/scada_bank_signal.schema.json`'s `value` description) and the guardian relies
        on for flow-direction checks (G-30). `apply_scada_tick` always writes the REAL point from the
        SCADA sim's own signed `REAL_POWER_KW` message via `set_signal` -- this method must never
        overwrite it with an unsigned guess afterward."""
        self.set_signal(bank_id, "APPARENT_POWER_KVA", kva, quality=quality)
        if not self.derive_electricals:
            return
        current_a = kva * 1000.0 / (math.sqrt(3) * _NOMINAL_LL_VOLTAGE_V)
        derived = {
            "VOLTAGE_PU": 1.0,
            "CURRENT_A": current_a,
            "VOLTAGE_A_PU": 1.0,
            "VOLTAGE_B_PU": 1.0,
            "VOLTAGE_C_PU": 1.0,
            "CURRENT_A_PHASE_A": current_a,
            "CURRENT_A_PHASE_B": current_a,
            "CURRENT_A_PHASE_C": current_a,
            "FREQUENCY_HZ": _NOMINAL_HZ,
            "THD_V_PCT": 1.5,
            "THD_I_PCT": 4.0,
        }
        for signal, value in derived.items():
            self.set_signal(bank_id, signal, value, quality=quality)

    def set_instruction(self, bank_id: str, kind: str | None, *, limit_kw: float | None = None) -> None:
        """Utility instruction as levels: `kind` in {LIMIT, BLOCK, ESTOP} or None to clear all."""
        self.set_status(bank_id, "UTILITY_ESTOP", kind == "ESTOP")
        self.set_status(bank_id, "UTILITY_BLOCK", kind == "BLOCK")
        if kind == "LIMIT" and limit_kw is not None:
            self.set_signal(bank_id, "UTILITY_LIMIT_KW", limit_kw)
        self.set_status(bank_id, "UTILITY_LIMIT_ACTIVE", kind == "LIMIT")

    def apply_scada_tick(
        self, signals: list[tuple[str, dict[str, Any]]], instructions: list[tuple[str, dict[str, Any]]]
    ) -> int:
        """Apply one `ScadaEngine.tick()` output; returns how many messages were applied."""
        applied = 0
        for _topic, msg in signals:
            bank_id = msg["bank_id"]
            if bank_id not in self._position:
                continue
            if msg["signal"] == "APPARENT_POWER_KVA":
                self.set_bank_kva(bank_id, float(msg["value"]), quality=msg["quality"])
            elif msg["signal"] in _SIGNAL_OFFSET:
                self.set_signal(bank_id, msg["signal"], float(msg["value"]), quality=msg["quality"])
            applied += 1
        for _topic, msg in instructions:
            bank_id = msg["bank_id"]
            if bank_id not in self._position:
                continue
            lifted = msg.get("expires_at") is not None and msg.get("expires_at") == msg.get("issued_at")
            self.set_instruction(bank_id, None if lifted else msg["kind"], limit_kw=msg.get("limit_kw"))
            applied += 1
        return applied

    # -- application layer ---------------------------------------------------------------------------

    def _iin(self) -> int:
        iin = 0
        for cls, bit in w.IIN_CLASS.items():
            if self.events[cls]:
                iin |= bit
        return iin

    @staticmethod
    def _static_blocks(group: int, points: dict[int, Point], size: int) -> list[bytes]:
        blocks: list[bytes] = []
        indices = sorted(points)
        per_block = max(1, (_MAX_FRAGMENT - 16) // size)
        run: list[int] = []
        for index in [*indices, -2]:
            if run and (index != run[-1] + 1 or len(run) >= per_block):
                body = b""
                for i in run:
                    p = points[i]
                    if group == 1:
                        body += bytes([p.flags | (STATE if p.value else 0)])
                    else:
                        body += bytes([p.flags]) + struct.pack("<i", int(p.value))
                variation = 2 if group == 1 else 1
                blocks.append(bytes([group, variation, 0x01]) + struct.pack("<HH", run[0], run[-1]) + body)
                run = []
            if index >= 0:
                run.append(index)
        return blocks

    def _event_blocks(self, classes: list[int]) -> list[bytes]:
        blocks: list[bytes] = []
        for cls in classes:
            events, self.events[cls] = self.events[cls], []
            binaries = [e for e in events if e[0] == w.BI_EVENT]
            analogs = [e for e in events if e[0] == w.AI_EVENT]
            for chunk_start in range(0, len(binaries), 300):
                chunk = binaries[chunk_start : chunk_start + 300]
                body = b"".join(
                    struct.pack("<H", i) + bytes([f | (STATE if v else 0)]) for _, i, v, f in chunk
                )
                blocks.append(bytes([2, 1, 0x28]) + struct.pack("<H", len(chunk)) + body)
            for chunk_start in range(0, len(analogs), 250):
                chunk = analogs[chunk_start : chunk_start + 250]
                body = b"".join(
                    struct.pack("<H", i) + bytes([f]) + struct.pack("<i", int(v)) for _, i, v, f in chunk
                )
                blocks.append(bytes([32, 1, 0x28]) + struct.pack("<H", len(chunk)) + body)
        return blocks

    def _fragments(self, seq: int, blocks: list[bytes], iin: int) -> list[bytes]:
        groups: list[list[bytes]] = [[]]
        size = 4
        for block in blocks:
            if groups[-1] and size + len(block) > _MAX_FRAGMENT:
                groups.append([])
                size = 4
            groups[-1].append(block)
            size += len(block)
        out: list[bytes] = []
        for n, group in enumerate(groups):
            first, last = n == 0, n == len(groups) - 1
            control = (w.FIR if first else 0) | (w.FIN if last else 0) | (0 if last else w.CON) | (seq & 0x0F)
            out.append(bytes([control, w.FC_RESPONSE]) + struct.pack("<H", iin) + b"".join(group))
            seq = (seq + 1) & 0x0F
        return out

    def process_request(self, fragment: bytes) -> list[bytes]:
        """One application request -> response fragments (empty for CONFIRM)."""
        if len(fragment) < 2:
            return []
        seq, function = fragment[0] & 0x0F, fragment[1]
        if function == w.FC_CONFIRM:
            return []
        if function != w.FC_READ:
            return self._fragments(seq, [], self._iin() | w.IIN_NO_FUNC)
        blocks: list[bytes] = []
        error = 0
        classes: list[int] = []
        static0 = False
        pos = 2
        while pos + 3 <= len(fragment):
            group, variation, qualifier = fragment[pos], fragment[pos + 1], fragment[pos + 2]
            pos += 3
            if qualifier != 0x06:
                error |= w.IIN_PARAM_ERROR
                break
            if group == 60 and variation == 1:
                static0 = True
            elif group == 60 and variation in (2, 3, 4):
                classes.append(variation - 1)
            elif group == 1:
                blocks += self._static_blocks(1, self.binaries, 1)
            elif group == 30:
                blocks += self._static_blocks(30, self.analogs, 5)
            else:
                error |= w.IIN_OBJECT_UNKNOWN
        blocks = self._event_blocks(classes) + blocks
        if static0:
            blocks += self._static_blocks(1, self.binaries, 1) + self._static_blocks(30, self.analogs, 5)
        return self._fragments(seq, blocks, self._iin() | error)

    # -- link / transport / TCP ------------------------------------------------------------------------

    async def start(self) -> tuple[str, int]:
        """Start listening; returns the bound (host, port) (port 0 binds an ephemeral port)."""
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        host, port = self._server.sockets[0].getsockname()[:2]
        return str(host), int(port)

    async def stop(self) -> None:
        for task in list(self._connections):
            task.cancel()
        for task in list(self._connections):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        parser, reassembler, tseq = w.LinkParser(), w.Reassembler(), 0

        async def send(control: int, data: bytes = b"") -> None:
            writer.write(w.build_link_frame(control, self.master_address, self.address, data))
            await writer.drain()

        try:
            while data := await reader.read(4096):
                for frame in parser.feed(data):
                    if frame.destination != self.address or not frame.control & w.PRM:
                        continue
                    function = frame.control & 0x0F
                    if function == w.PRI_LINK_STATUS_REQ:
                        await send(w.SEC_LINK_STATUS)
                        continue
                    if function in (w.PRI_RESET_LINK, w.PRI_TEST_LINK):
                        await send(w.SEC_ACK)
                        continue
                    if function == w.PRI_CONFIRMED_DATA:
                        await send(w.SEC_ACK)
                    elif function != w.PRI_UNCONFIRMED_DATA:
                        continue
                    request = reassembler.add(frame.data)
                    if request is None:
                        continue
                    for response in self.process_request(request):
                        segments, tseq = w.segment(response, tseq)
                        for seg in segments:
                            await send(w.PRM | w.PRI_UNCONFIRMED_DATA, seg)
        except (ConnectionError, OSError):
            return
        finally:
            writer.close()
            if task is not None:
                self._connections.discard(task)
