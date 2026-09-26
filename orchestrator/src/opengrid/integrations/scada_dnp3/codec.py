"""Minimal DNP3 (IEEE 1815-2012) master-side codec: exactly the subset the SCADA adapter needs.

Implemented here, with no third-party DNP3 dependency:

- **Data link layer** (IEEE 1815 S9): `0x05 0x64` start, length, control, destination and source
  (little-endian), header CRC, then user data in 16-byte blocks each followed by its CRC. CRC-16/DNP:
  polynomial 0x3D65 (reflected 0xA6BC), initial value 0, final complement, sent little-endian.
- **Transport function** (S8): one header byte (FIN 0x80, FIR 0x40, 6-bit sequence), at most 249 payload
  bytes per segment; reassembly into application fragments.
- **Application layer** (S4): READ requests for Class 0/1/2/3 (group 60, qualifier 0x06), CONFIRM, and
  parsing of RESPONSE / UNSOLICITED_RESPONSE fragments (control, function, IIN, object headers).
- **Objects parsed** (anything else stops the parse with `Dnp3ParseError`, never a guess):
  binary input g1v1 (packed) / g1v2 (flags), binary input event g2v1/v2/v3, analog input g30v1..v6,
  analog input event g32v1..v8, and the time objects g50v1 / g51v1 / g51v2 that may precede events.
  Qualifiers 0x00/0x01 (start-stop), 0x07/0x08 (count) and 0x17/0x28 (count with index prefix).
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass, field

__all__ = [
    "BINARY_STATE",
    "FLAG_COMM_LOST",
    "FLAG_ONLINE",
    "FLAG_OVER_RANGE",
    "FLAG_REFERENCE_ERR",
    "FLAG_RESTART",
    "AnalogReading",
    "AppFragment",
    "BinaryReading",
    "Dnp3ParseError",
    "LinkFrame",
    "LinkParser",
    "Reassembler",
    "build_confirm",
    "build_link_frame",
    "build_read_classes",
    "crc16_dnp",
    "parse_response",
    "segment_fragment",
]

# point quality flags (IEEE 1815 Table 11-1 etc.)
FLAG_ONLINE = 0x01
FLAG_RESTART = 0x02
FLAG_COMM_LOST = 0x04
FLAG_OVER_RANGE = 0x20
FLAG_REFERENCE_ERR = 0x40
BINARY_STATE = 0x80

# link layer control
DIR = 0x80
PRM = 0x40
LINK_RESET_LINK_STATES = 0x00
LINK_TEST_LINK_STATES = 0x02
LINK_CONFIRMED_USER_DATA = 0x03
LINK_UNCONFIRMED_USER_DATA = 0x04
LINK_REQUEST_LINK_STATUS = 0x09
LINK_ACK = 0x00

# application layer
APP_FIR = 0x80
APP_FIN = 0x40
APP_CON = 0x20
APP_UNS = 0x10
FC_CONFIRM = 0x00
FC_READ = 0x01
FC_RESPONSE = 0x81
FC_UNSOLICITED_RESPONSE = 0x82

_START = b"\x05\x64"
_MAX_USER_DATA = 250
_MAX_SEGMENT_PAYLOAD = 249


class Dnp3ParseError(ValueError):
    """A frame, segment or fragment that does not decode."""


def _crc_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA6BC if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


_CRC_TABLE = _crc_table()


def crc16_dnp(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ byte) & 0xFF]
    return ~crc & 0xFFFF


def _with_crc(block: bytes) -> bytes:
    return block + crc16_dnp(block).to_bytes(2, "little")


# -- data link -----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LinkFrame:
    control: int
    destination: int
    source: int
    data: bytes = b""

    @property
    def is_primary(self) -> bool:
        return bool(self.control & PRM)

    @property
    def function(self) -> int:
        return self.control & 0x0F


def build_link_frame(control: int, destination: int, source: int, data: bytes = b"") -> bytes:
    if len(data) > _MAX_USER_DATA:
        raise ValueError("DNP3 link user data is at most 250 bytes")
    header = _START + bytes([5 + len(data), control]) + struct.pack("<HH", destination, source)
    out = bytearray(_with_crc(header))
    for i in range(0, len(data), 16):
        out += _with_crc(data[i : i + 16])
    return bytes(out)


class LinkParser:
    """Incremental frame parser: feed bytes, get CRC-valid frames; resynchronises on garbage."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[LinkFrame]:
        self._buf += data
        frames: list[LinkFrame] = []
        while True:
            start = self._buf.find(_START)
            if start < 0:
                del self._buf[: max(0, len(self._buf) - 1)]
                return frames
            del self._buf[:start]
            if len(self._buf) < 10:
                return frames
            header = bytes(self._buf[:8])
            if int.from_bytes(self._buf[8:10], "little") != crc16_dnp(header) or header[2] < 5:
                del self._buf[:2]  # bad header: skip this start sequence
                continue
            user_len = header[2] - 5
            total = 10 + user_len + 2 * ((user_len + 15) // 16)
            if len(self._buf) < total:
                return frames
            user_data = bytearray()
            offset = 10
            ok = True
            remaining = user_len
            while remaining > 0:
                size = min(16, remaining)
                block = bytes(self._buf[offset : offset + size])
                if int.from_bytes(self._buf[offset + size : offset + size + 2], "little") != crc16_dnp(block):
                    ok = False
                    break
                user_data += block
                offset += size + 2
                remaining -= size
            del self._buf[:total]
            if ok:
                destination, source = struct.unpack("<HH", header[4:8])
                frames.append(
                    LinkFrame(
                        control=header[3], destination=destination, source=source, data=bytes(user_data)
                    )
                )


# -- transport -----------------------------------------------------------------------------------------


def segment_fragment(fragment: bytes, start_seq: int) -> tuple[list[bytes], int]:
    """Split an application fragment into transport segments; returns (segments, next sequence)."""
    segments: list[bytes] = []
    seq = start_seq
    chunks = [
        fragment[i : i + _MAX_SEGMENT_PAYLOAD] for i in range(0, len(fragment), _MAX_SEGMENT_PAYLOAD)
    ] or [b""]
    for i, chunk in enumerate(chunks):
        header = (0x40 if i == 0 else 0) | (0x80 if i == len(chunks) - 1 else 0) | (seq & 0x3F)
        segments.append(bytes([header]) + chunk)
        seq = (seq + 1) & 0x3F
    return segments, seq


class Reassembler:
    """Transport reassembly: FIR starts a fragment, FIN completes it, sequence gaps discard it."""

    def __init__(self, max_fragment: int = 2048) -> None:
        self._buf: bytearray | None = None
        self._next_seq = 0
        self._max = max_fragment

    def add(self, segment: bytes) -> bytes | None:
        if not segment:
            return None
        header, payload = segment[0], segment[1:]
        fir, fin, seq = bool(header & 0x40), bool(header & 0x80), header & 0x3F
        if fir:
            self._buf = bytearray()
        elif self._buf is None or seq != self._next_seq:
            self._buf = None  # out of sequence: drop the partial fragment
            return None
        self._buf += payload
        self._next_seq = (seq + 1) & 0x3F
        if len(self._buf) > self._max:
            self._buf = None
            raise Dnp3ParseError("application fragment exceeds the configured maximum")
        if fin:
            fragment = bytes(self._buf)
            self._buf = None
            return fragment
        return None


# -- application ---------------------------------------------------------------------------------------


def build_read_classes(seq: int, *, class0: bool, class1: bool, class2: bool, class3: bool) -> bytes:
    """READ request for the given classes (group 60 variations 2,3,4 = class 1,2,3; 1 = class 0).
    Events are listed before static data, as an integrity poll requires."""
    objects = b""
    for flag, variation in ((class1, 2), (class2, 3), (class3, 4), (class0, 1)):
        if flag:
            objects += bytes([60, variation, 0x06])
    return bytes([APP_FIR | APP_FIN | (seq & 0x0F), FC_READ]) + objects


def build_confirm(seq: int, *, unsolicited: bool) -> bytes:
    return bytes([APP_FIR | APP_FIN | (APP_UNS if unsolicited else 0) | (seq & 0x0F), FC_CONFIRM])


@dataclass(frozen=True, slots=True)
class AnalogReading:
    index: int
    value: float
    flags: int


@dataclass(frozen=True, slots=True)
class BinaryReading:
    index: int
    value: bool
    flags: int


@dataclass
class AppFragment:
    fir: bool
    fin: bool
    con: bool
    uns: bool
    seq: int
    function: int
    iin: int
    analogs: list[AnalogReading] = field(default_factory=list)
    binaries: list[BinaryReading] = field(default_factory=list)


# (group, variation) -> (object size in bytes, decoder kind)
_ANALOG_LAYOUTS: dict[tuple[int, int], tuple[int, str, int]] = {
    # (size, value format, flags present 1/0); event variations with time carry 6 extra bytes
    (30, 1): (5, "<i", 1),
    (30, 2): (3, "<h", 1),
    (30, 3): (4, "<i", 0),
    (30, 4): (2, "<h", 0),
    (30, 5): (5, "<f", 1),
    (30, 6): (9, "<d", 1),
    (32, 1): (5, "<i", 1),
    (32, 2): (3, "<h", 1),
    (32, 3): (11, "<i", 1),
    (32, 4): (9, "<h", 1),
    (32, 5): (5, "<f", 1),
    (32, 6): (9, "<d", 1),
    (32, 7): (11, "<f", 1),
    (32, 8): (15, "<d", 1),
}
_BINARY_SIZES: dict[tuple[int, int], int] = {(1, 2): 1, (2, 1): 1, (2, 2): 7, (2, 3): 3}
_TIME_SIZES: dict[tuple[int, int], int] = {(50, 1): 6, (51, 1): 6, (51, 2): 6}


def _ranges(qualifier: int, data: bytes, pos: int) -> tuple[list[int] | None, int, int, int]:
    """Decode the range field: (indices or None when index-prefixed, count, prefix size, new pos)."""
    code, prefix = qualifier & 0x0F, (qualifier >> 4) & 0x07
    if code in (0x00, 0x01):
        width = 1 if code == 0x00 else 2
        if pos + 2 * width > len(data):
            raise Dnp3ParseError("truncated start/stop range")
        start = int.from_bytes(data[pos : pos + width], "little")
        stop = int.from_bytes(data[pos + width : pos + 2 * width], "little")
        if stop < start:
            raise Dnp3ParseError("stop index before start index")
        return list(range(start, stop + 1)), stop - start + 1, 0, pos + 2 * width
    if code in (0x07, 0x08):
        width = 1 if code == 0x07 else 2
        if pos + width > len(data):
            raise Dnp3ParseError("truncated count")
        count = int.from_bytes(data[pos : pos + width], "little")
        prefix_size = {0: 0, 1: 1, 2: 2}.get(prefix)
        if prefix_size is None:
            raise Dnp3ParseError(f"unsupported qualifier 0x{qualifier:02X}")
        return (None if prefix_size else list(range(count))), count, prefix_size, pos + width
    raise Dnp3ParseError(f"unsupported qualifier 0x{qualifier:02X}")


def _items(
    data: bytes, pos: int, indices: list[int] | None, count: int, prefix: int, size: int
) -> Iterator[tuple[int, bytes]]:
    for n in range(count):
        if prefix:
            index = int.from_bytes(data[pos : pos + prefix], "little")
            pos += prefix
        else:
            index = indices[n] if indices is not None else n
        if pos + size > len(data):
            raise Dnp3ParseError("truncated object")
        yield index, data[pos : pos + size]
        pos += size


def parse_response(fragment: bytes) -> AppFragment:
    """Decode a RESPONSE / UNSOLICITED_RESPONSE application fragment."""
    if len(fragment) < 4:
        raise Dnp3ParseError("response shorter than its header")
    control, function = fragment[0], fragment[1]
    if function not in (FC_RESPONSE, FC_UNSOLICITED_RESPONSE):
        raise Dnp3ParseError(f"not a response (function 0x{function:02X})")
    out = AppFragment(
        fir=bool(control & APP_FIR),
        fin=bool(control & APP_FIN),
        con=bool(control & APP_CON),
        uns=bool(control & APP_UNS),
        seq=control & 0x0F,
        function=function,
        iin=int.from_bytes(fragment[2:4], "little"),
    )
    pos = 4
    while pos < len(fragment):
        if pos + 3 > len(fragment):
            raise Dnp3ParseError("truncated object header")
        group, variation, qualifier = fragment[pos], fragment[pos + 1], fragment[pos + 2]
        indices, count, prefix, pos = _ranges(qualifier, fragment, pos + 3)
        key = (group, variation)
        if key == (1, 1):  # packed binary inputs, 1 bit each
            if indices is None:
                raise Dnp3ParseError("g1v1 cannot be index-prefixed")
            nbytes = (count + 7) // 8
            packed = fragment[pos : pos + nbytes]
            if len(packed) < nbytes:
                raise Dnp3ParseError("truncated g1v1")
            for n, index in enumerate(indices):
                state = bool(packed[n // 8] >> (n % 8) & 1)
                out.binaries.append(BinaryReading(index, state, FLAG_ONLINE | (BINARY_STATE if state else 0)))
            pos += nbytes
        elif key in _BINARY_SIZES:
            size = _BINARY_SIZES[key]
            for index, raw in _items(fragment, pos, indices, count, prefix, size):
                out.binaries.append(BinaryReading(index, bool(raw[0] & BINARY_STATE), raw[0]))
            pos += count * (size + prefix)
        elif key in _ANALOG_LAYOUTS:
            size, fmt, has_flags = _ANALOG_LAYOUTS[key]
            width = struct.calcsize(fmt)
            for index, raw in _items(fragment, pos, indices, count, prefix, size):
                flags = raw[0] if has_flags else FLAG_ONLINE
                (value,) = struct.unpack(fmt, raw[has_flags : has_flags + width])
                out.analogs.append(AnalogReading(index, float(value), flags))
            pos += count * (size + prefix)
        elif key in _TIME_SIZES:
            pos += count * (_TIME_SIZES[key] + prefix)  # time / CTO objects: not needed by the adapter
        else:
            raise Dnp3ParseError(f"unsupported object g{group}v{variation}")
        if pos > len(fragment):
            raise Dnp3ParseError("object data past the end of the fragment")
    return out
