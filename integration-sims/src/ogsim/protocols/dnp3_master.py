"""Minimal DNP3 (IEEE 1815-2012) MASTER for the simulated utility EMS: it drives OpenGrid's grid-link
outstation the way a control centre does (grid-link.md S4). ogsim's own implementation on `dnp3_wire`;
it shares no code with opengrid.

Supported: READ class 0 (integrity), SELECT/OPERATE and DIRECT_OPERATE of g12v1 CROBs and g41v1 (int32)
analog outputs with qualifier 0x17, application CONFIRM of multi-fragment responses, and parsing of the
g1v2 / g30v1 / g30v5 static objects (qualifiers 0x01/0x28) and of the g12/g41 control echoes. TCP or
mutual TLS. One request at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import struct
from dataclasses import dataclass, field

from ogsim.protocols import dnp3_wire as w

__all__ = ["Dnp3Master", "Dnp3MasterError", "PointValues", "client_ssl_context"]

FC_SELECT, FC_OPERATE, FC_DIRECT_OPERATE = 0x03, 0x04, 0x05
_READ_CHUNK = 4096


class Dnp3MasterError(Exception):
    """Connection, timeout or framing failure."""


@dataclass
class PointValues:
    analogs: dict[int, float] = field(default_factory=dict)
    analog_flags: dict[int, int] = field(default_factory=dict)
    binaries: dict[int, bool] = field(default_factory=dict)
    iin: int = 0


def client_ssl_context(ca_file: str, cert_file: str, key_file: str) -> ssl.SSLContext:
    """Mutual-TLS client context (the EMS side of IEC 62351-3)."""
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file)
    return context


def _range(qualifier: int, data: bytes, pos: int) -> tuple[list[int] | None, int, int, int]:
    code, prefix = qualifier & 0x0F, (qualifier >> 4) & 0x07
    if code in (0x00, 0x01):
        width = 1 if code == 0x00 else 2
        start = int.from_bytes(data[pos : pos + width], "little")
        stop = int.from_bytes(data[pos + width : pos + 2 * width], "little")
        return list(range(start, stop + 1)), stop - start + 1, 0, pos + 2 * width
    if code in (0x07, 0x08):
        width = 1 if code == 0x07 else 2
        count = int.from_bytes(data[pos : pos + width], "little")
        return None, count, prefix, pos + width
    raise Dnp3MasterError(f"unsupported qualifier 0x{qualifier:02X}")


_SIZES = {(1, 2): 1, (30, 1): 5, (30, 5): 5, (12, 1): 11, (41, 1): 5, (41, 2): 3, (41, 3): 5}


def parse_objects(fragment: bytes, out: PointValues, statuses: list[int]) -> None:
    """Decode a response fragment's objects into `out` (inputs) and `statuses` (control echoes)."""
    out.iin |= int.from_bytes(fragment[2:4], "little")
    pos = 4
    while pos + 3 <= len(fragment):
        group, variation, qualifier = fragment[pos], fragment[pos + 1], fragment[pos + 2]
        indices, count, prefix, pos = _range(qualifier, fragment, pos + 3)
        size = _SIZES.get((group, variation))
        if size is None:
            raise Dnp3MasterError(f"unsupported object g{group}v{variation}")
        for n in range(count):
            if prefix:
                index = int.from_bytes(fragment[pos : pos + prefix], "little")
                pos += prefix
            else:
                index = indices[n] if indices is not None else n
            raw = fragment[pos : pos + size]
            pos += size
            if group == 1:
                out.binaries[index] = bool(raw[0] & 0x80)
            elif group == 30:
                fmt = "<f" if variation == 5 else "<i"
                out.analogs[index] = float(struct.unpack(fmt, raw[1:5])[0])
                out.analog_flags[index] = raw[0]
            else:
                statuses.append(raw[-1])


class Dnp3Master:
    """One association from this EMS master to the grid-link outstation."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        master_address: int = 1,
        outstation_address: int = 10,
        ssl_context: ssl.SSLContext | None = None,
        server_hostname: str | None = None,
        timeout_s: float = 5.0,
    ) -> None:
        self.host, self.port = host, port
        self.master_address, self.outstation_address = master_address, outstation_address
        self._ssl = ssl_context
        self._server_hostname = server_hostname if ssl_context is not None else None
        self.timeout_s = timeout_s
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._parser = w.LinkParser()
        self._tseq = 0
        self._aseq = 0
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.host, self.port, ssl=self._ssl, server_hostname=self._server_hostname
                ),
                timeout=self.timeout_s,
            )
        except (OSError, TimeoutError, ssl.SSLError) as exc:
            raise Dnp3MasterError(f"connect failed: {exc}") from exc
        self._parser, self._tseq = w.LinkParser(), 0

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError, ssl.SSLError, ConnectionError):
                await writer.wait_closed()

    # -- operations --------------------------------------------------------------------------------------

    async def integrity_poll(self) -> PointValues:
        values = PointValues()
        await self._request(0x01, bytes([60, 1, 0x06]), values, [])
        return values

    async def analog_output(self, index: int, value: int, *, sbo: bool = False) -> int:
        body = bytes([41, 1, 0x17, 1, index]) + struct.pack("<i", int(value)) + b"\x00"
        return await self._operate(body, sbo=sbo)

    async def crob(self, index: int, code: int, *, sbo: bool = False) -> int:
        body = bytes([12, 1, 0x17, 1, index, code, 1]) + struct.pack("<II", 0, 0) + b"\x00"
        return await self._operate(body, sbo=sbo)

    async def _operate(self, body: bytes, *, sbo: bool) -> int:
        if sbo:
            statuses: list[int] = []
            await self._request(FC_SELECT, body, PointValues(), statuses)
            if not statuses or statuses[0] != 0:
                return statuses[0] if statuses else -1
            statuses = []
            await self._request(FC_OPERATE, body, PointValues(), statuses)
        else:
            statuses = []
            await self._request(FC_DIRECT_OPERATE, body, PointValues(), statuses)
        return statuses[0] if statuses else -1

    # -- link / transport ------------------------------------------------------------------------------

    async def _send(self, fragment: bytes) -> None:
        if self._writer is None:
            raise Dnp3MasterError("not connected")
        segments, self._tseq = w.segment(fragment, self._tseq)
        for seg in segments:
            control = w.DIR | w.PRM | w.PRI_UNCONFIRMED_DATA
            self._writer.write(w.build_link_frame(control, self.outstation_address, self.master_address, seg))
        try:
            await self._writer.drain()
        except (OSError, ConnectionError) as exc:
            raise Dnp3MasterError(f"write failed: {exc}") from exc

    async def _request(self, function: int, body: bytes, out: PointValues, statuses: list[int]) -> None:
        async with self._lock:
            seq = self._aseq
            self._aseq = (seq + 1) & 0x0F
            await self._send(bytes([w.FIR | w.FIN | seq, function]) + body)
            await self._collect(out, statuses)

    async def _collect(self, out: PointValues, statuses: list[int]) -> None:
        reassembler = w.Reassembler()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s
        while True:
            if self._reader is None:
                raise Dnp3MasterError("not connected")
            try:
                data = await asyncio.wait_for(self._reader.read(_READ_CHUNK), timeout=deadline - loop.time())
            except (TimeoutError, ValueError) as exc:
                raise Dnp3MasterError("response timeout") from exc
            except (OSError, ConnectionError, ssl.SSLError) as exc:
                raise Dnp3MasterError(f"read failed: {exc}") from exc
            if not data:
                raise Dnp3MasterError("outstation closed the connection")
            for frame in self._parser.feed(data):
                if frame.destination != self.master_address or not frame.control & w.PRM:
                    continue
                fragment = reassembler.add(frame.data)
                if fragment is None or len(fragment) < 4 or fragment[1] != w.FC_RESPONSE:
                    continue
                parse_objects(fragment, out, statuses)
                if fragment[0] & w.CON:
                    await self._send(bytes([w.FIR | w.FIN | (fragment[0] & 0x0F), w.FC_CONFIRM]))
                if fragment[0] & w.FIN:
                    return
                deadline = loop.time() + self.timeout_s
