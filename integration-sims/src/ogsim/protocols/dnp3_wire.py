"""Minimal DNP3 (IEEE 1815-2012) OUTSTATION wire codec for the ogsim DNP3 simulator.

Independent of the orchestrator's master codec (BUILD.md S1: ogsim shares no code with opengrid), so
the end-to-end tests exercise two separate implementations of the same standard:

- data link: `0x05 0x64` frames, CRC-16/DNP (poly 0x3D65 reflected 0xA6BC, init 0, complemented,
  little-endian) over the 8-byte header and every 16-byte data block;
- transport: FIR/FIN/6-bit sequence header, 249-byte segments;
- application: READ of Class 0/1/2/3 (g60v1..v4) and of g1/g30 static (any variation, qualifier 0x06),
  CONFIRM (no reply), anything else answered with IIN2 "function code not supported". Responses carry
  g1v2 / g30v1 static data (qualifier 0x01) and g2v1 / g32v1 events (qualifier 0x28).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

__all__ = [
    "AI_EVENT",
    "BI_EVENT",
    "LinkFrame",
    "LinkParser",
    "Reassembler",
    "build_link_frame",
    "crc16_dnp",
    "segment",
]

_START = b"\x05\x64"

# link functions
PRI_RESET_LINK = 0x00
PRI_TEST_LINK = 0x02
PRI_CONFIRMED_DATA = 0x03
PRI_UNCONFIRMED_DATA = 0x04
PRI_LINK_STATUS_REQ = 0x09
SEC_ACK = 0x00
SEC_LINK_STATUS = 0x0B
PRM = 0x40
DIR = 0x80

# application
FIR, FIN, CON, UNS = 0x80, 0x40, 0x20, 0x10
FC_CONFIRM, FC_READ, FC_RESPONSE = 0x00, 0x01, 0x81
IIN_CLASS = {1: 0x0002, 2: 0x0004, 3: 0x0008}
IIN_NO_FUNC = 0x0100
IIN_OBJECT_UNKNOWN = 0x0200
IIN_PARAM_ERROR = 0x0400
BI_EVENT = "BI"
AI_EVENT = "AI"


def _table() -> list[int]:
    out = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA6BC if crc & 1 else crc >> 1
        out.append(crc)
    return out


_CRC = _table()


def crc16_dnp(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = (crc >> 8) ^ _CRC[(crc ^ byte) & 0xFF]
    return ~crc & 0xFFFF


def _crc_block(block: bytes) -> bytes:
    return block + struct.pack("<H", crc16_dnp(block))


@dataclass(frozen=True)
class LinkFrame:
    control: int
    destination: int
    source: int
    data: bytes


def build_link_frame(control: int, destination: int, source: int, data: bytes = b"") -> bytes:
    header = _START + bytes([5 + len(data), control]) + struct.pack("<HH", destination, source)
    out = _crc_block(header)
    for i in range(0, len(data), 16):
        out += _crc_block(data[i : i + 16])
    return out


class LinkParser:
    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[LinkFrame]:
        self.buf += data
        frames: list[LinkFrame] = []
        while True:
            i = self.buf.find(_START)
            if i < 0:
                del self.buf[:-1]
                return frames
            del self.buf[:i]
            if len(self.buf) < 10:
                return frames
            header = bytes(self.buf[:8])
            if struct.unpack("<H", self.buf[8:10])[0] != crc16_dnp(header) or header[2] < 5:
                del self.buf[:2]
                continue
            n = header[2] - 5
            total = 10 + n + 2 * ((n + 15) // 16)
            if len(self.buf) < total:
                return frames
            data, pos, ok = b"", 10, True
            while len(data) < n:
                size = min(16, n - len(data))
                block = bytes(self.buf[pos : pos + size])
                if struct.unpack("<H", self.buf[pos + size : pos + size + 2])[0] != crc16_dnp(block):
                    ok = False
                    break
                data += block
                pos += size + 2
            del self.buf[:total]
            if ok:
                dst, src = struct.unpack("<HH", header[4:8])
                frames.append(LinkFrame(header[3], dst, src, data))


class Reassembler:
    def __init__(self) -> None:
        self.buf: bytearray | None = None
        self.next_seq = 0

    def add(self, seg: bytes) -> bytes | None:
        if not seg:
            return None
        head = seg[0]
        if head & 0x40:
            self.buf = bytearray()
        elif self.buf is None or (head & 0x3F) != self.next_seq:
            self.buf = None
            return None
        self.buf += seg[1:]
        self.next_seq = (head + 1) & 0x3F
        if head & 0x80:
            out, self.buf = bytes(self.buf), None
            return out
        return None


def segment(fragment: bytes, seq: int) -> tuple[list[bytes], int]:
    chunks = [fragment[i : i + 249] for i in range(0, len(fragment), 249)] or [b""]
    out = []
    for i, chunk in enumerate(chunks):
        out.append(bytes([(0x40 if i == 0 else 0) | (0x80 if i == len(chunks) - 1 else 0) | seq]) + chunk)
        seq = (seq + 1) & 0x3F
    return out, seq
