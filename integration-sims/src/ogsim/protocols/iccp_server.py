"""ICCP/TASE.2 control-centre simulator over the documented SIMULATOR transport (protocol-adapters.md S5.3).

This is not MMS on the wire. It reproduces the TASE.2 operations a client performs against a utility
control centre -- associate under a bilateral table, read data values, start a DS transfer set, receive
periodic transfer reports, conclude -- as length-prefixed JSON over TCP (4-byte big-endian length, then
a UTF-8 JSON object with an `op` field, at most 1 MiB).

Access control follows the bilateral table: an association with an unknown bilateral-table id or the
wrong domain is rejected, and a read of a data value the table does not grant returns
`object-access-denied` for that name. Values follow TASE.2 typing: reals are engineering floats;
states are 2-bit (0 BETWEEN, 1 OFF, 2 ON, 3 INVALID); every value carries Validity and CurrentSource.

Point naming matches `ogsim.protocols.dnp3_outstation`'s bank points: `<BANK>_<SUFFIX>`, e.g.
`BANK_000_KVA`, `BANK_000_ESTOP`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
from dataclasses import dataclass, field
from typing import Any

__all__ = ["IccpServerSim", "standard_point_names"]

_HEADER = struct.Struct(">I")
_MAX_MESSAGE = 1 << 20

_REAL_SUFFIXES = (
    "KVA", "KW", "VPU", "AMP", "VA_PU", "VB_PU", "VC_PU", "IA", "IB", "IC", "HZ", "THDV", "THDI", "LIMKW",
)  # fmt: skip
_STATE_SUFFIXES = ("COMM", "BRKR", "BLOCK", "ESTOP", "LIMACT")
STATE_OFF = 1
STATE_ON = 2


def standard_point_names(bank_ids: list[str]) -> dict[str, str]:
    """name -> TASE.2 type for the standard OpenGrid bank points."""
    names: dict[str, str] = {}
    for bank_id in bank_ids:
        prefix = bank_id.upper().replace("-", "_")
        names.update({f"{prefix}_{s}": "Data_RealQ" for s in _REAL_SUFFIXES})
        names.update({f"{prefix}_{s}": "Data_StateQ" for s in _STATE_SUFFIXES})
    return names


@dataclass
class _Point:
    type: str
    value: float
    validity: str = "VALID"
    current_source: str = "TELEMETERED"

    def wire(self, name: str) -> dict[str, Any]:
        return {
            "name": name,
            "type": self.type,
            "value": self.value,
            "quality": {"validity": self.validity, "current_source": self.current_source},
        }


@dataclass
class IccpServerSim:
    """A utility control centre exposing bank points to one partner under one bilateral table."""

    bank_ids: list[str]
    bilateral_table_id: str = "BLT-OPENGRID-01"
    server_domain: str = "UTILITY_ICC"
    client_domain: str = "OPENGRID_ICC"
    host: str = "127.0.0.1"
    port: int = 0
    points: dict[str, _Point] = field(default_factory=dict)
    granted: set[str] = field(default_factory=set)
    associations: int = 0
    _server: asyncio.Server | None = None
    _connections: set[asyncio.Task[None]] = field(default_factory=set)

    def __post_init__(self) -> None:
        for name, tase2_type in standard_point_names(self.bank_ids).items():
            initial = 0.0
            validity = "NOTVALID"  # never written yet
            if name.endswith(("_COMM", "_BRKR")):
                initial, validity = float(STATE_ON), "VALID"
            elif name.endswith(("_BLOCK", "_ESTOP", "_LIMACT")):
                initial, validity = float(STATE_OFF), "VALID"
            self.points[name] = _Point(type=tase2_type, value=initial, validity=validity)
        if not self.granted:
            self.granted = set(self.points)

    # -- point writes --------------------------------------------------------------------------------

    def set_value(self, name: str, value: float, *, validity: str = "VALID") -> None:
        point = self.points[name]
        point.value = value
        point.validity = validity

    def set_state(self, name: str, on: bool) -> None:
        self.set_value(name, float(STATE_ON if on else STATE_OFF))

    def set_bank_kva(self, bank_id: str, kva: float, *, validity: str = "VALID") -> None:
        self.set_value(f"{bank_id.upper().replace('-', '_')}_KVA", kva, validity=validity)

    def set_instruction(self, bank_id: str, kind: str | None, *, limit_kw: float | None = None) -> None:
        prefix = bank_id.upper().replace("-", "_")
        self.set_state(f"{prefix}_ESTOP", kind == "ESTOP")
        self.set_state(f"{prefix}_BLOCK", kind == "BLOCK")
        if kind == "LIMIT" and limit_kw is not None:
            self.set_value(f"{prefix}_LIMKW", limit_kw)
        self.set_state(f"{prefix}_LIMACT", kind == "LIMIT")

    # -- server --------------------------------------------------------------------------------------

    async def start(self) -> tuple[str, int]:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        sockname = self._server.sockets[0].getsockname()
        return str(sockname[0]), int(sockname[1])

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
        reporter: asyncio.Task[None] | None = None
        associated = False
        try:
            while True:
                (length,) = _HEADER.unpack(await reader.readexactly(_HEADER.size))
                if length > _MAX_MESSAGE:
                    return
                message = json.loads(await reader.readexactly(length))
                op = message.get("op")
                if op == "associate":
                    ok = (
                        message.get("bilateral_table_id") == self.bilateral_table_id
                        and message.get("local_domain") == self.client_domain
                        and message.get("remote_domain") == self.server_domain
                    )
                    if not ok:
                        reject = {
                            "op": "error",
                            "code": "association-rejected",
                            "detail": "bilateral table mismatch",
                        }
                        await self._send(writer, reject)
                        return
                    associated = True
                    self.associations += 1
                    await self._send(
                        writer, {"op": "associate_ok", "tase2_version": message.get("tase2_version")}
                    )
                elif not associated:
                    await self._send(writer, {"op": "error", "code": "not-associated", "detail": op})
                    return
                elif op == "read":
                    values, errors = self._read(list(message.get("names") or []))
                    await self._send(writer, {"op": "read_response", "values": values, "errors": errors})
                elif op == "start_transfer_set":
                    names = list(message.get("names") or [])
                    _, errors = self._read(names)
                    if errors:
                        await self._send(writer, {"op": "error", "code": "object-access-denied",
                                                  "detail": sorted(errors)[:5]})  # fmt: skip
                        continue
                    if reporter is not None:
                        reporter.cancel()
                    interval = max(0.01, float(message.get("interval_s") or 2.0))
                    reporter = asyncio.create_task(self._report_loop(writer, names, interval))
                    await self._send(
                        writer, {"op": "transfer_set_started", "ts_name": message.get("dataset")}
                    )
                elif op == "conclude":
                    await self._send(writer, {"op": "conclude_ok"})
                    return
                else:
                    await self._send(writer, {"op": "error", "code": "service-not-supported", "detail": op})
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            return
        finally:
            if reporter is not None:
                reporter.cancel()
            writer.close()
            if task is not None:
                self._connections.discard(task)

    def _read(self, names: list[str]) -> tuple[list[dict[str, Any]], dict[str, str]]:
        values: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        for name in names:
            if name not in self.points:
                errors[name] = "object-non-existent"
            elif name not in self.granted:
                errors[name] = "object-access-denied"
            else:
                values.append(self.points[name].wire(name))
        return values, errors

    async def _report_loop(self, writer: asyncio.StreamWriter, names: list[str], interval: float) -> None:
        with contextlib.suppress(ConnectionError, OSError):
            while True:
                values, _ = self._read(names)
                await self._send(writer, {"op": "transfer_report", "values": values})
                await asyncio.sleep(interval)

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        body = json.dumps(message).encode()
        writer.write(_HEADER.pack(len(body)) + body)
        await writer.drain()
