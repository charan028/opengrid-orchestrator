"""DNP3 outstation application layer for the grid link: one session per master association (grid-link.md
S4.2). Pure request -> response logic; the TCP/TLS and link handling live in `dnp3_server`.

Implemented (IEEE 1815-2012 subset; everything else is answered with IIN2 bits, never guessed):

- READ, qualifier 0x06 (all): g60v1 (class 0 -> static g1v2 + g30v5), g60v2..4 (no events are kept: an
  empty answer), g30v0/v1/v5, g1v0/v2. Static analogs and binaries go out with qualifier 0x28.
- SELECT / OPERATE (select-before-operate, matching object bytes, sequence select+1, within
  `select_timeout_s`), DIRECT_OPERATE and DIRECT_OPERATE_NR, for g12v1 (CROB) and g41v1/v2/v3 (AO),
  qualifiers 0x17/0x28. Each object is echoed with its control status.
- CONFIRM (application layer) is handled by the server loop (multi-fragment responses).

Control semantics (opengrid-gridlink-v1): the three call AOs are STAGING registers held per session; the
CALL_EXECUTE CROB turns the staged values into a `TollCall` and clears them. Staged values older than the
select timeout are refused (TIMEOUT), so a stale setpoint can never be executed later.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from opengrid.integrations.grid_link.config import Dnp3LinkSettings
from opengrid.integrations.grid_link.model import (
    CancelCall,
    ControlVerdict,
    GridCommand,
    Heartbeat,
    L2Block,
    L2Limit,
    L2LimitValue,
    LinkStatus,
    TollCall,
)
from opengrid.integrations.grid_link.points import ControlRole, GridLinkPointMap
from opengrid.integrations.scada_dnp3.codec import (
    APP_CON,
    APP_FIN,
    APP_FIR,
    BINARY_STATE,
    FC_CONFIRM,
    FC_DIRECT_OPERATE,
    FC_DIRECT_OPERATE_NR,
    FC_OPERATE,
    FC_READ,
    FC_RESPONSE,
    FC_SELECT,
    FLAG_COMM_LOST,
    FLAG_ONLINE,
    Dnp3ParseError,
    decode_range,
    iter_items,
)

__all__ = ["CONTROL_STATUS", "ControlStatus", "OutstationSession"]

# IIN (IIN1 low byte, IIN2 high byte, little-endian on the wire)
IIN_NO_FUNC = 0x0100
IIN_OBJECT_UNKNOWN = 0x0200
IIN_PARAM_ERROR = 0x0400

ControlStatus = int
#: IEEE 1815-2012 Table 11-4 control status codes used here.
CONTROL_STATUS: dict[ControlVerdict, ControlStatus] = {
    ControlVerdict.ACCEPTED: 0,
    ControlVerdict.TIMEOUT: 1,
    ControlVerdict.FORMAT_ERROR: 3,
    ControlVerdict.NOT_SUPPORTED: 4,
    ControlVerdict.NOT_AUTHORIZED: 9,
    ControlVerdict.INHIBITED: 10,
    ControlVerdict.OUT_OF_RANGE: 12,
}
STATUS_NO_SELECT = 2
_ON_CODES = frozenset({0x01, 0x03, 0x41})  # PULSE_ON, LATCH_ON, CLOSE/PULSE_ON
_OFF_CODES = frozenset({0x02, 0x04, 0x81})  # PULSE_OFF, LATCH_OFF, TRIP/PULSE_ON
_CROB_SIZE = 11
_AO_LAYOUTS: dict[int, tuple[str, int]] = {1: ("<i", 5), 2: ("<h", 3), 3: ("<f", 5)}
_MAX_CALL_ID = 2**31 - 1
_STAGED_ROLES = ("CALL_SETPOINT_KW", "CALL_DURATION_MIN", "CALL_ID")

Offer = Callable[[GridCommand], ControlVerdict]


@dataclass
class _Staging:
    values: dict[str, tuple[float, float]] = field(default_factory=dict)  # role -> (value, monotonic)

    def copy(self) -> _Staging:
        return _Staging(dict(self.values))


@dataclass(frozen=True, slots=True)
class _Control:
    kind: Literal["CROB", "AO"]
    index: int
    raw: bytes  # the object bytes after the index prefix (echoed back with the status patched)
    value: float = 0.0
    code: int = 0


@dataclass(frozen=True, slots=True)
class _Block:
    group: int
    variation: int
    qualifier: int
    prefix: int
    controls: tuple[_Control, ...]


@dataclass
class _Selected:
    seq: int
    body: bytes
    at: float


class OutstationSession:
    """Application-layer state of ONE master association."""

    def __init__(
        self,
        points: GridLinkPointMap,
        settings: Dnp3LinkSettings,
        *,
        validate: Offer,
        offer: Offer,
        status: Callable[[], LinkStatus],
        monotonic: Callable[[], float],
        max_setpoint_kw: float,
        max_duration_min: int,
    ) -> None:
        self._points = points
        self._settings = settings
        self._validate = validate
        self._offer = offer
        self._status = status
        self._monotonic = monotonic
        self._max_setpoint_kw = max_setpoint_kw
        self._max_duration_min = max_duration_min
        self._staging = _Staging()
        self._selected: _Selected | None = None

    # -- entry point -------------------------------------------------------------------------------------

    def process(self, fragment: bytes) -> list[bytes]:
        """One request fragment -> response fragments (empty for CONFIRM and DIRECT_OPERATE_NR)."""
        if len(fragment) < 2:
            return []
        seq, function, body = fragment[0] & 0x0F, fragment[1], fragment[2:]
        if function == FC_CONFIRM:
            return []
        try:
            if function == FC_READ:
                return self._read(seq, body)
            if function in (FC_SELECT, FC_OPERATE, FC_DIRECT_OPERATE, FC_DIRECT_OPERATE_NR):
                response = self._control(seq, function, body)
                return [] if function == FC_DIRECT_OPERATE_NR else [response]
        except Dnp3ParseError:
            return [self._fragment(seq, b"", IIN_PARAM_ERROR)]
        return [self._fragment(seq, b"", IIN_NO_FUNC)]

    # -- READ -----------------------------------------------------------------------------------------------

    def _read(self, seq: int, body: bytes) -> list[bytes]:
        status = self._status()
        blocks: list[bytes] = []
        iin = 0
        pos = 0
        while pos + 3 <= len(body):
            group, variation, qualifier = body[pos], body[pos + 1], body[pos + 2]
            pos += 3
            if qualifier != 0x06:
                iin |= IIN_PARAM_ERROR
                break
            if (group, variation) == (60, 1):
                blocks += self._binaries(status) + self._analogs(status, 5)
            elif group == 60 and variation in (2, 3, 4):
                continue  # no event buffer: every read of class 1/2/3 is empty by design
            elif group == 30 and variation in (0, 1, 5):
                blocks += self._analogs(status, 1 if variation == 1 else 5)
            elif group == 1 and variation in (0, 2):
                blocks += self._binaries(status)
            else:
                iin |= IIN_OBJECT_UNKNOWN
        return self._fragments(seq, blocks, iin)

    def _chunked(self, header: bytes, objects: list[bytes]) -> list[bytes]:
        """Object blocks (qualifier 0x28) of at most one fragment's worth of objects each."""
        if not objects:
            return []
        room = self._settings.max_fragment_size - 4 - len(header) - 2
        per_block = max(1, room // len(objects[0]))
        return [
            header + struct.pack("<H", len(chunk)) + b"".join(chunk)
            for chunk in (objects[i : i + per_block] for i in range(0, len(objects), per_block))
        ]

    def _analogs(self, status: LinkStatus, variation: int) -> list[bytes]:
        values = self._points.analog_inputs(status)
        objects: list[bytes] = []
        for index in self._points.analog_input_indices():
            value = values.get(index)
            flags = FLAG_ONLINE if value is not None else FLAG_COMM_LOST
            encoded = (
                struct.pack("<f", value or 0.0) if variation == 5 else struct.pack("<i", round(value or 0))
            )
            objects.append(struct.pack("<H", index) + bytes([flags]) + encoded)
        return self._chunked(bytes([30, variation, 0x28]), objects)

    def _binaries(self, status: LinkStatus) -> list[bytes]:
        values = self._points.binary_inputs(status)
        objects = [
            struct.pack("<H", index) + bytes([FLAG_ONLINE | (BINARY_STATE if values.get(index) else 0)])
            for index in self._points.binary_input_indices()
        ]
        return self._chunked(bytes([1, 2, 0x28]), objects)

    def _fragments(self, seq: int, blocks: list[bytes], iin: int) -> list[bytes]:
        limit = self._settings.max_fragment_size - 4
        groups: list[bytes] = [b""]
        for block in blocks:
            if groups[-1] and len(groups[-1]) + len(block) > limit:
                groups.append(b"")
            groups[-1] += block
        out: list[bytes] = []
        for n, payload in enumerate(groups):
            first, last = n == 0, n == len(groups) - 1
            control = (APP_FIR if first else 0) | (APP_FIN if last else 0) | (0 if last else APP_CON)
            out.append(self._fragment(seq, payload, iin, control=control))
            seq = (seq + 1) & 0x0F
        return out

    @staticmethod
    def _fragment(seq: int, payload: bytes, iin: int, *, control: int = APP_FIR | APP_FIN) -> bytes:
        return bytes([control | (seq & 0x0F), FC_RESPONSE]) + struct.pack("<H", iin) + payload

    # -- controls ---------------------------------------------------------------------------------------

    def _control(self, seq: int, function: int, body: bytes) -> bytes:
        blocks = _parse_controls(body)
        now = self._monotonic()
        if function == FC_SELECT:
            statuses = self._run(blocks, commit=False, now=now, direct=False)
            if all(s == 0 for block in statuses for s in block):
                self._selected = _Selected(seq, body, now)
            return self._echo(seq, blocks, statuses)
        if function == FC_OPERATE:
            selected, self._selected = self._selected, None
            fresh = selected is not None and now - selected.at <= self._settings.select_timeout_s
            if not (
                fresh and selected is not None and selected.body == body and seq == (selected.seq + 1) & 0x0F
            ):
                return self._echo(seq, blocks, [[STATUS_NO_SELECT] * len(b.controls) for b in blocks])
            return self._echo(seq, blocks, self._run(blocks, commit=True, now=now, direct=False))
        return self._echo(seq, blocks, self._run(blocks, commit=True, now=now, direct=True))

    def _run(self, blocks: list[_Block], *, commit: bool, now: float, direct: bool) -> list[list[int]]:
        staging = self._staging if commit else self._staging.copy()
        results: list[list[int]] = []
        for block in blocks:
            results.append(
                [
                    self._one(control, staging, commit=commit, now=now, direct=direct)
                    for control in block.controls
                ]
            )
        return results

    def _one(self, control: _Control, staging: _Staging, *, commit: bool, now: float, direct: bool) -> int:
        role = (
            self._points.binary_output_role(control.index)
            if control.kind == "CROB"
            else self._points.analog_output_role(control.index)
        )
        if role is None:
            return CONTROL_STATUS[ControlVerdict.NOT_SUPPORTED]
        if role.name in _STAGED_ROLES:
            verdict = self._stage(role, control.value, staging, now)
            return CONTROL_STATUS[verdict]
        if (
            direct
            and self._settings.control_mode == "sbo_only"
            and role.name in ("CALL_EXECUTE", "CALL_CANCEL")
        ):
            return CONTROL_STATUS[ControlVerdict.NOT_AUTHORIZED]
        command = self._command(role, control, staging, now)
        if isinstance(command, ControlVerdict):
            return CONTROL_STATUS[command]
        verdict = self._offer(command) if commit else self._validate(command)
        if commit and verdict is ControlVerdict.ACCEPTED and isinstance(command, TollCall):
            staging.values.clear()
        return CONTROL_STATUS[verdict]

    def _stage(self, role: ControlRole, value: float, staging: _Staging, now: float) -> ControlVerdict:
        valid = {
            "CALL_SETPOINT_KW": 0 < value <= self._max_setpoint_kw,
            "CALL_DURATION_MIN": 1 <= value <= self._max_duration_min and value == int(value),
            "CALL_ID": 1 <= value <= _MAX_CALL_ID and value == int(value),
        }[role.name]
        if not valid:
            return ControlVerdict.OUT_OF_RANGE
        staging.values[role.name] = (value, now)
        return ControlVerdict.ACCEPTED

    def _staged(self, staging: _Staging, role: str, now: float) -> float | ControlVerdict:
        entry = staging.values.get(role)
        if entry is None:
            return ControlVerdict.FORMAT_ERROR
        value, at = entry
        return value if now - at <= self._settings.select_timeout_s else ControlVerdict.TIMEOUT

    def _command(
        self, role: ControlRole, control: _Control, staging: _Staging, now: float
    ) -> GridCommand | ControlVerdict:
        on = control.code in _ON_CODES
        if control.kind == "CROB" and not on and control.code not in _OFF_CODES:
            return ControlVerdict.NOT_SUPPORTED
        target = role.target or ""
        match role.name:
            case "HEARTBEAT":
                return Heartbeat()
            case "L2_LIMIT_KW":
                return L2LimitValue(target, control.value)
            case "L2_LIMIT_ACTIVE":
                return L2Limit(target, on)
            case "L2_BLOCK":
                return L2Block(target, on)
            case "CALL_CANCEL":
                if not on:
                    return ControlVerdict.NOT_SUPPORTED
                call_id = self._staged(staging, "CALL_ID", now)
                return CancelCall(int(call_id) if isinstance(call_id, float) else None)
            case "CALL_EXECUTE":
                return self._toll_call(staging, now) if on else ControlVerdict.NOT_SUPPORTED
        return ControlVerdict.NOT_SUPPORTED

    def _toll_call(self, staging: _Staging, now: float) -> GridCommand | ControlVerdict:
        staged = [self._staged(staging, role, now) for role in _STAGED_ROLES]
        for item in staged:
            if isinstance(item, ControlVerdict):
                return item
        setpoint, duration, call_id = (float(item) for item in staged)
        return TollCall(ems_call_id=int(call_id), setpoint_kw=setpoint, duration_min=int(duration))

    def _echo(self, seq: int, blocks: list[_Block], statuses: list[list[int]]) -> bytes:
        payload = bytearray()
        for block, block_status in zip(blocks, statuses, strict=True):
            payload += bytes([block.group, block.variation, block.qualifier])
            width = 1 if block.qualifier == 0x17 else 2
            payload += len(block.controls).to_bytes(width, "little")
            for control, status in zip(block.controls, block_status, strict=True):
                payload += control.index.to_bytes(block.prefix, "little") + control.raw[:-1] + bytes([status])
        return self._fragment(seq, bytes(payload), 0)


def _parse_controls(body: bytes) -> list[_Block]:
    """Decode g12v1 / g41v1-3 object blocks with qualifier 0x17 or 0x28; anything else raises."""
    blocks: list[_Block] = []
    pos = 0
    while pos < len(body):
        if pos + 3 > len(body):
            raise Dnp3ParseError("truncated object header")
        group, variation, qualifier = body[pos], body[pos + 1], body[pos + 2]
        if qualifier not in (0x17, 0x28):
            raise Dnp3ParseError(f"unsupported control qualifier 0x{qualifier:02X}")
        _indices, count, prefix, pos = decode_range(qualifier, body, pos + 3)
        if (group, variation) == (12, 1):
            size, fmt = _CROB_SIZE, ""
        elif group == 41 and variation in _AO_LAYOUTS:
            fmt, size = _AO_LAYOUTS[variation]
        else:
            raise Dnp3ParseError(f"unsupported control object g{group}v{variation}")
        controls = []
        for index, raw in iter_items(body, pos, None, count, prefix, size):
            if group == 12:
                controls.append(_Control("CROB", index, raw, code=raw[0]))
            else:
                (value,) = struct.unpack(fmt, raw[: struct.calcsize(fmt)])
                controls.append(_Control("AO", index, raw, value=float(value)))
        pos += count * (size + prefix)
        blocks.append(_Block(group, variation, qualifier, prefix, tuple(controls)))
    return blocks
