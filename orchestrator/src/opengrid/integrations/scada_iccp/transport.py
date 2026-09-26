"""ICCP transports (protocol-adapters.md S5.3).

`IccpTransport` is the seam between the bilateral-table mapping (ours) and the MMS stack (not ours):

- `SimTcpIccpTransport`: the SIMULATOR transport. It exchanges the same TASE.2 operations and data blocks
  (associate with bilateral-table id, read data values, define and start a DS transfer set, receive
  transfer reports, conclude) as length-prefixed JSON over TCP (optionally TLS). It is what ogsim's
  ICCP server speaks. It is NOT ICCP on the wire.
- `MmsIccpTransport`: the production seat. Real TASE.2 runs over ISO/OSI MMS (ISO 9506) on RFC 1006
  (TCP 102), usually with TLS per IEC 62351-4/-6. Python has no maintained, certified TASE.2 client, so
  this class fails loudly until a vendor stack (e.g. a TASE.2 SDK exposing read / DS transfer set
  callbacks) is bound behind it. The mapping and adapter do not change when that happens.

Wire format of the simulator transport: every message is a 4-byte big-endian length followed by a UTF-8
JSON object (at most 1 MiB), with an `op` field. See the protocol-adapters doc for the full op table.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
import struct
from dataclasses import dataclass
from typing import Any, Protocol

from opengrid.integrations.scada_iccp.bilateral import BilateralTable

__all__ = [
    "IccpError",
    "IccpTransport",
    "IccpValue",
    "MmsIccpTransport",
    "SimTcpIccpTransport",
]

_MAX_MESSAGE = 1 << 20
_HEADER = struct.Struct(">I")


class IccpError(Exception):
    """Association, access or transport failure on a TASE.2 link."""


@dataclass(frozen=True, slots=True)
class IccpValue:
    name: str
    value: float
    validity: str = "VALID"
    current_source: str = "TELEMETERED"


class IccpTransport(Protocol):
    async def associate(self, table: BilateralTable) -> None: ...

    async def read(self, names: list[str]) -> list[IccpValue]: ...

    async def start_transfer_set(self, table: BilateralTable) -> None: ...

    async def next_report(self, timeout_s: float) -> list[IccpValue]: ...

    async def conclude(self) -> None: ...


def _values(raw: list[dict[str, Any]]) -> list[IccpValue]:
    out: list[IccpValue] = []
    for item in raw:
        quality = item.get("quality") or {}
        out.append(
            IccpValue(
                name=str(item["name"]),
                value=float(item["value"]),
                validity=str(quality.get("validity", "VALID")),
                current_source=str(quality.get("current_source", "TELEMETERED")),
            )
        )
    return out


class SimTcpIccpTransport:
    """Simulator transport (module docstring). One outstanding request at a time; transfer reports
    are queued as they arrive."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout_s: float = 5.0,
        ssl_context: ssl.SSLContext | None = None,
        server_hostname: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout_s = timeout_s
        self._ssl = ssl_context
        self._server_hostname = server_hostname if ssl_context is not None else None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pump: asyncio.Task[None] | None = None
        self._responses: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._reports: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._closed_reason: str | None = None

    @property
    def connected(self) -> bool:
        return self._writer is not None and self._pump is not None and not self._pump.done()

    async def _open(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self._host, self._port, ssl=self._ssl, server_hostname=self._server_hostname
                ),
                timeout=self._timeout_s,
            )
        except (OSError, TimeoutError) as exc:
            raise IccpError(f"connect {self._host}:{self._port} failed: {exc}") from exc
        self._responses = asyncio.Queue()
        self._reports = asyncio.Queue()
        self._closed_reason = None
        self._pump = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        reader = self._reader
        if reader is None:
            return
        try:
            while True:
                header = await reader.readexactly(_HEADER.size)
                (length,) = _HEADER.unpack(header)
                if length > _MAX_MESSAGE:
                    raise IccpError("oversized ICCP message")
                message = json.loads(await reader.readexactly(length))
                if not isinstance(message, dict):
                    raise IccpError("malformed ICCP message")
                queue = self._reports if message.get("op") == "transfer_report" else self._responses
                queue.put_nowait(message)
        except (asyncio.IncompleteReadError, OSError, ValueError, IccpError) as exc:
            self._closed_reason = str(exc) or type(exc).__name__
            self._responses.put_nowait({"op": "_closed"})
            self._reports.put_nowait({"op": "_closed"})

    async def _send(self, message: dict[str, Any]) -> None:
        if self._writer is None:
            raise IccpError("not associated")
        body = json.dumps(message).encode()
        try:
            self._writer.write(_HEADER.pack(len(body)) + body)
            await self._writer.drain()
        except OSError as exc:
            raise IccpError(f"write failed: {exc}") from exc

    async def _request(self, message: dict[str, Any], expect: str) -> dict[str, Any]:
        async with self._lock:
            await self._send(message)
            try:
                reply = await asyncio.wait_for(self._responses.get(), timeout=self._timeout_s)
            except TimeoutError as exc:
                raise IccpError(f"timeout waiting for {expect}") from exc
        if reply.get("op") == "_closed":
            raise IccpError(f"connection closed: {self._closed_reason}")
        if reply.get("op") == "error":
            raise IccpError(f"{reply.get('code')}: {reply.get('detail')}")
        if reply.get("op") != expect:
            raise IccpError(f"unexpected reply {reply.get('op')!r}, wanted {expect!r}")
        return reply

    async def associate(self, table: BilateralTable) -> None:
        await self._open()
        await self._request(
            {
                "op": "associate",
                "bilateral_table_id": table.bilateral_table_id,
                "tase2_version": table.tase2_version,
                "local_domain": table.local_domain,
                "remote_domain": table.remote_domain,
                "calling_ap_title": table.calling_ap_title,
                "called_ap_title": table.called_ap_title,
            },
            "associate_ok",
        )

    async def read(self, names: list[str]) -> list[IccpValue]:
        reply = await self._request({"op": "read", "names": names}, "read_response")
        errors = reply.get("errors") or {}
        if errors:
            raise IccpError(f"read refused for {sorted(errors)[:5]}: {sorted(set(errors.values()))}")
        return _values(reply.get("values") or [])

    async def start_transfer_set(self, table: BilateralTable) -> None:
        await self._request(
            {
                "op": "start_transfer_set",
                "dataset": table.dataset_name,
                "names": [v.name for v in table.data_values],
                "interval_s": table.transfer_interval_s,
                "rbe": table.report_by_exception,
            },
            "transfer_set_started",
        )

    async def next_report(self, timeout_s: float) -> list[IccpValue]:
        try:
            message = await asyncio.wait_for(self._reports.get(), timeout=timeout_s)
        except TimeoutError as exc:
            raise IccpError("no transfer report within timeout") from exc
        if message.get("op") == "_closed":
            raise IccpError(f"connection closed: {self._closed_reason}")
        return _values(message.get("values") or [])

    async def conclude(self) -> None:
        if self.connected:
            with contextlib.suppress(IccpError):
                await self._request({"op": "conclude"}, "conclude_ok")
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError, ssl.SSLError):
                await writer.wait_closed()
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
            self._pump = None


class MmsIccpTransport:
    """Production TASE.2 over MMS. Requires a vendor TASE.2/MMS stack bound here (see module docstring
    and protocol-adapters.md S5.4); until then every call raises so the gap can never be silent."""

    _MESSAGE = (
        "ICCP/TASE.2 over MMS needs a vendor TASE.2 stack, the utility's bilateral table and IEC 62351 "
        "certificates; bind them in MmsIccpTransport (protocol-adapters.md S5.4)"
    )

    async def associate(self, table: BilateralTable) -> None:
        raise NotImplementedError(self._MESSAGE)

    async def read(self, names: list[str]) -> list[IccpValue]:
        raise NotImplementedError(self._MESSAGE)

    async def start_transfer_set(self, table: BilateralTable) -> None:
        raise NotImplementedError(self._MESSAGE)

    async def next_report(self, timeout_s: float) -> list[IccpValue]:
        raise NotImplementedError(self._MESSAGE)

    async def conclude(self) -> None:
        return None
