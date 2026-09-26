"""DNP3 master adapter: polls utility outstations and maps points to `ScadaBankSignal` and
`ScadaUtilityInstruction` (protocol-adapters.md S3).

Polling (IEEE 1815-2012 Level 2 master behaviour):

- on connect, and every `integrity_poll_s`: integrity poll (Class 1, 2, 3 events, then Class 0 static);
- every `event_poll_s` in between: event poll (Class 1, 2, 3); unsolicited responses are confirmed and
  applied as they arrive;
- after every successful poll, every mapped analog of every bank is delivered with `ts = now` (the
  outstation reports by exception, so an unchanged value is still the current value while the
  association is healthy); utility instruction points go through the shared change tracker.

Failure behaviour: a failed poll delivers NOTHING (the association is closed and retried with capped
exponential backoff). Downstream staleness handling (health's SCADA freshness, the PI loop's open-loop
fallback, guardian G-03's missing-reading veto) then applies exactly as when the MQTT SCADA goes silent.

Quality: DNP3 point flags map onto `ScadaBankSignal.quality` (`dnp3_quality`): COMM_LOST -> `comm_fail`;
RESTART (never updated) -> `missing`; not ONLINE -> `comm_fail`; OVER_RANGE/REFERENCE_ERR ->
`out_of_range`; otherwise `good`. Unusable qualities are withheld (`opengrid.integrations.bank_points`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Hashable
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from opengrid.integrations.bank_points import BankPointAssembler, MappedPoint, Quality
from opengrid.integrations.interfaces import ScadaSink
from opengrid.integrations.scada_dnp3.channel import Dnp3ChannelError, Dnp3MasterChannel
from opengrid.integrations.scada_dnp3.codec import (
    FLAG_COMM_LOST,
    FLAG_ONLINE,
    FLAG_OVER_RANGE,
    FLAG_REFERENCE_ERR,
    FLAG_RESTART,
    Dnp3ParseError,
    build_read_classes,
    parse_response,
)
from opengrid.integrations.scada_dnp3.points import Dnp3PointMap, standard_layout
from opengrid.integrations.tls import TlsSettings, build_client_ssl_context

logger = logging.getLogger(__name__)

__all__ = ["Dnp3OutstationSettings", "Dnp3ScadaSource", "Dnp3Settings", "dnp3_quality"]


class Dnp3OutstationSettings(BaseModel):
    """One outstation (one DNP3 association). `banks` expands to `standard_layout(banks)` when
    `points` is not given explicitly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    host: str
    port: int = Field(default=20000, ge=1, le=65535)
    master_address: int = Field(default=1, ge=0, le=65519)
    outstation_address: int = Field(default=10, ge=0, le=65519)
    banks: list[str] = []
    points: Dnp3PointMap | None = None
    tls: TlsSettings = TlsSettings()
    reset_link_on_connect: bool = False

    def point_map(self) -> Dnp3PointMap:
        return self.points if self.points is not None else standard_layout(self.banks)


class Dnp3Settings(BaseModel):
    """`[integrations.scada.dnp3]`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outstations: list[Dnp3OutstationSettings] = Field(min_length=1)
    integrity_poll_s: float = Field(default=60.0, gt=0)
    event_poll_s: float = Field(default=2.0, gt=0)
    response_timeout_s: float = Field(default=5.0, gt=0)
    connect_timeout_s: float = Field(default=5.0, gt=0)
    reconnect_min_s: float = Field(default=1.0, gt=0)
    reconnect_max_s: float = Field(default=30.0, gt=0)
    breaker_open_blocks: bool = True  # an open bank breaker is treated as a utility BLOCK (fail safe)
    deliver_bad_quality: bool = False
    issued_by: str = "UTILITY_SCADA_DNP3"


def dnp3_quality(flags: int) -> Quality:
    """DNP3 analog point flags -> `ScadaBankSignal.quality` (module docstring)."""
    if flags & FLAG_COMM_LOST:
        return "comm_fail"
    if flags & FLAG_RESTART:
        return "missing"
    if not flags & FLAG_ONLINE:
        return "comm_fail"
    if flags & (FLAG_OVER_RANGE | FLAG_REFERENCE_ERR):
        return "out_of_range"
    return "good"


class _OutstationPoller:
    """Association state for one outstation: channel, master sequence state, point assembler."""

    def __init__(self, cfg: Dnp3OutstationSettings, settings: Dnp3Settings) -> None:
        self.cfg = cfg
        points = cfg.point_map()
        keyed: dict[Hashable, MappedPoint] = {("AI", p.index): p.mapped() for p in points.analogs}
        keyed.update({("BI", p.index): p.mapped() for p in points.binaries})
        self.assembler = BankPointAssembler(
            keyed,
            source=f"dnp3:{cfg.name}",
            issued_by=settings.issued_by,
            breaker_open_blocks=settings.breaker_open_blocks,
            deliver_bad_quality=settings.deliver_bad_quality,
        )
        self._app_seq = 0
        self.channel = Dnp3MasterChannel(
            cfg.host,
            cfg.port,
            master_address=cfg.master_address,
            outstation_address=cfg.outstation_address,
            response_timeout_s=settings.response_timeout_s,
            connect_timeout_s=settings.connect_timeout_s,
            ssl_context=build_client_ssl_context(cfg.tls),
            server_hostname=cfg.tls.server_hostname,
            reset_link_on_connect=cfg.reset_link_on_connect,
        )
        self.channel.on_unsolicited = self._on_unsolicited

    def _on_unsolicited(self, fragment: bytes) -> None:
        try:
            self._apply(fragment)
        except Dnp3ParseError:
            logger.warning(
                "dropped unparseable unsolicited DNP3 response", extra={"outstation": self.cfg.name}
            )

    def _apply(self, fragment: bytes) -> None:
        response = parse_response(fragment)
        for a in response.analogs:
            self.assembler.update_analog(("AI", a.index), a.value, dnp3_quality(a.flags))
        for b in response.binaries:
            online = bool(b.flags & FLAG_ONLINE) and not b.flags & FLAG_COMM_LOST
            self.assembler.update_binary(("BI", b.index), b.value, online=online)

    def _next_seq(self) -> int:
        seq = self._app_seq
        self._app_seq = (seq + 1) & 0x0F
        return seq

    async def ensure_connected(self) -> bool:
        """Connect if needed; True when a (re)connect happened (caller then runs an integrity poll)."""
        if self.channel.connected:
            return False
        await self.channel.connect()
        return True

    async def integrity_poll(self) -> None:
        await self._exchange(
            build_read_classes(self._next_seq(), class0=True, class1=True, class2=True, class3=True)
        )

    async def event_poll(self) -> None:
        await self._exchange(
            build_read_classes(self._next_seq(), class0=False, class1=True, class2=True, class3=True)
        )

    async def _exchange(self, request: bytes) -> None:
        for fragment in await self.channel.request(request):
            try:
                self._apply(fragment)
            except Dnp3ParseError as exc:
                raise Dnp3ChannelError(f"unparseable DNP3 response: {exc}") from exc


class Dnp3ScadaSource:
    """`ScadaSource` over DNP3 (one or more outstations, each its own association)."""

    def __init__(self, settings: Dnp3Settings, *, clock: Callable[[], datetime] | None = None) -> None:
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._pollers = [_OutstationPoller(cfg, settings) for cfg in settings.outstations]

    @property
    def backend(self) -> str:
        return "dnp3"

    async def _deliver(self, poller: _OutstationPoller, sink: ScadaSink) -> int:
        now = self._clock()
        count = 0
        for signal in poller.assembler.signals(now):
            await sink.on_bank_signal(signal)
            count += 1
        for instruction in poller.assembler.instructions(now):
            await sink.on_utility_instruction(instruction)
            count += 1
        return count

    async def poll_once(self, sink: ScadaSink) -> int:
        """Integrity-poll every outstation once and deliver. Raises `Dnp3ChannelError` on failure."""
        total = 0
        for poller in self._pollers:
            await poller.ensure_connected()
            await poller.integrity_poll()
            total += await self._deliver(poller, sink)
        return total

    async def event_poll_once(self, sink: ScadaSink) -> int:
        """Event-poll (Class 1/2/3) every outstation once and deliver (integrity poll on reconnect)."""
        total = 0
        for poller in self._pollers:
            if await poller.ensure_connected():
                await poller.integrity_poll()
            else:
                await poller.event_poll()
            total += await self._deliver(poller, sink)
        return total

    async def run(self, sink: ScadaSink) -> None:
        async with asyncio.TaskGroup() as group:
            for poller in self._pollers:
                group.create_task(self._run_one(poller, sink))

    async def _run_one(self, poller: _OutstationPoller, sink: ScadaSink) -> None:
        loop = asyncio.get_running_loop()
        backoff = self._settings.reconnect_min_s
        next_integrity = 0.0
        while True:
            try:
                if await poller.ensure_connected() or loop.time() >= next_integrity:
                    await poller.integrity_poll()
                    next_integrity = loop.time() + self._settings.integrity_poll_s
                else:
                    await poller.event_poll()
                await self._deliver(poller, sink)
                backoff = self._settings.reconnect_min_s
                await asyncio.sleep(self._settings.event_poll_s)
            except Exception as exc:  # any failure: drop the association and retry; never kill the task
                if isinstance(exc, Dnp3ChannelError):
                    logger.warning(
                        "dnp3 poll failed; reconnecting",
                        extra={"outstation": poller.cfg.name, "error": str(exc), "retry_s": backoff},
                    )
                else:
                    logger.exception("dnp3 poll raised; reconnecting", extra={"outstation": poller.cfg.name})
                await poller.channel.close()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._settings.reconnect_max_s)

    async def close(self) -> None:
        for poller in self._pollers:
            await poller.channel.close()
