"""ICCP/TASE.2 SCADA adapter (protocol-adapters.md S5).

Associates with the utility's control centre under the bilateral table, defines and starts a DS
transfer set over every agreed data value, and turns each transfer report into bank signals and utility
instructions through the shared point assembler. A missing report for `report_timeout_factor` x the
transfer interval, a refused association or a dropped connection concludes the association and
retries with capped exponential backoff; nothing is delivered meanwhile (staleness handling applies).

`transport = "sim_tcp"` uses the simulator transport (ogsim's ICCP server); `transport = "mms"` is the
production seat and raises until a vendor TASE.2 stack is bound (see `transport.py`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Hashable
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from opengrid.integrations.bank_points import BankPointAssembler, MappedPoint
from opengrid.integrations.interfaces import ScadaSink
from opengrid.integrations.scada_iccp.bilateral import (
    BilateralTable,
    iccp_quality,
    iccp_state_is_on,
)
from opengrid.integrations.scada_iccp.transport import (
    IccpError,
    IccpTransport,
    IccpValue,
    MmsIccpTransport,
    SimTcpIccpTransport,
)
from opengrid.integrations.tls import TlsSettings, build_client_ssl_context

logger = logging.getLogger(__name__)

__all__ = ["IccpScadaSource", "IccpSettings"]


class IccpSettings(BaseModel):
    """`[integrations.scada.iccp]`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transport: Literal["sim_tcp", "mms"] = "sim_tcp"
    host: str
    port: int = Field(default=102, ge=1, le=65535)  # RFC 1006 for real MMS
    timeout_s: float = Field(default=5.0, gt=0)
    report_timeout_factor: float = Field(default=3.0, ge=1)
    reconnect_min_s: float = Field(default=1.0, gt=0)
    reconnect_max_s: float = Field(default=30.0, gt=0)
    breaker_open_blocks: bool = True
    deliver_bad_quality: bool = False
    issued_by: str = "UTILITY_ICCP"
    tls: TlsSettings = TlsSettings()
    bilateral_table: BilateralTable


class IccpScadaSource:
    """`ScadaSource` over ICCP/TASE.2."""

    def __init__(
        self,
        settings: IccpSettings,
        *,
        transport_factory: Callable[[], IccpTransport] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._table = settings.bilateral_table
        self._clock = clock or (lambda: datetime.now(UTC))
        self._factory = transport_factory or self._default_factory
        self._transport: IccpTransport | None = None
        self._by_name = {v.name: v for v in self._table.data_values}
        keyed: dict[Hashable, MappedPoint] = {v.name: v.mapped() for v in self._table.data_values}
        self._assembler = BankPointAssembler(
            keyed,
            source=f"iccp:{self._table.bilateral_table_id}",
            issued_by=settings.issued_by,
            breaker_open_blocks=settings.breaker_open_blocks,
            deliver_bad_quality=settings.deliver_bad_quality,
        )

    def _default_factory(self) -> IccpTransport:
        if self._settings.transport == "mms":
            return MmsIccpTransport()
        return SimTcpIccpTransport(
            self._settings.host,
            self._settings.port,
            timeout_s=self._settings.timeout_s,
            ssl_context=build_client_ssl_context(self._settings.tls),
            server_hostname=self._settings.tls.server_hostname,
        )

    @property
    def backend(self) -> str:
        return "iccp"

    async def _associate(self) -> IccpTransport:
        if self._transport is None:
            transport = self._factory()
            try:
                await transport.associate(self._table)
            except BaseException:
                await transport.conclude()
                raise
            self._transport = transport
        return self._transport

    def _apply(self, values: list[IccpValue]) -> None:
        for item in values:
            data_value = self._by_name.get(item.name)
            if data_value is None:
                continue  # not in our bilateral table: ignore (never guess a mapping)
            if data_value.is_real:
                self._assembler.update_analog(item.name, item.value, iccp_quality(item.validity))
            else:
                value, online = iccp_state_is_on(int(item.value))
                online = online and iccp_quality(item.validity) == "good"
                self._assembler.update_binary(item.name, value, online=online)

    async def _deliver(self, sink: ScadaSink) -> int:
        now = self._clock()
        count = 0
        for signal in self._assembler.signals(now):
            await sink.on_bank_signal(signal)
            count += 1
        for instruction in self._assembler.instructions(now):
            await sink.on_utility_instruction(instruction)
            count += 1
        return count

    async def poll_once(self, sink: ScadaSink) -> int:
        """Associate if needed, read every agreed data value once, deliver."""
        transport = await self._associate()
        self._apply(await transport.read([v.name for v in self._table.data_values]))
        return await self._deliver(sink)

    async def run(self, sink: ScadaSink) -> None:
        backoff = self._settings.reconnect_min_s
        report_timeout = self._table.transfer_interval_s * self._settings.report_timeout_factor
        while True:
            try:
                transport = await self._associate()
                await transport.start_transfer_set(self._table)
                while True:
                    self._apply(await transport.next_report(report_timeout))
                    await self._deliver(sink)
                    backoff = self._settings.reconnect_min_s
            except NotImplementedError:
                raise  # the MMS seat without a vendor stack: a configuration error, never retried quietly
            except Exception as exc:  # any other failure: conclude and re-associate; never kill the task
                if isinstance(exc, IccpError):
                    logger.warning(
                        "iccp association failed; retrying", extra={"error": str(exc), "retry_s": backoff}
                    )
                else:
                    logger.exception("iccp adapter raised; retrying")
                await self.close()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._settings.reconnect_max_s)

    async def close(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.conclude()
