"""ogsim.scada over the utility grid-control link: the simulated utility DMS sends its L2 LIMIT / BLOCK
instructions to OpenGrid as DNP3 controls, the way a real EMS does (grid-link.md S4.3), instead of (or in
addition to) the `<root>/scada/instruction/<bank>` MQTT messages.

Config (`scada.yaml`):

    grid_link:
      enabled: false
      host: 127.0.0.1
      port: 20001
      master_address: 1
      outstation_address: 10
      heartbeat_s: 5
      targets: {bank-040: 0, bank-041: 1}   # bank -> L2 target position in OpenGrid's point list
      tls: {ca_file: ..., cert_file: ..., key_file: ..., server_hostname: ...}   # optional
      also_mqtt: true                      # keep publishing instructions on MQTT too

Mapping per instruction message (the same dicts `ScadaEngine.tick` produces):

- LIMIT -> AO `16 + t` = limit kW (int), then CROB `16 + 2t` LATCH_ON; a lift (`expires_at == issued_at`)
  -> CROB `16 + 2t` LATCH_OFF;
- BLOCK -> CROB `17 + 2t` LATCH_ON / LATCH_OFF;
- ESTOP is not a grid-link point: it stays on the MQTT path (logged).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ogsim.protocols.dnp3_master import Dnp3Master, Dnp3MasterError, client_ssl_context
from ogsim.protocols.gridlink_points import (
    AO_TARGET_BASE,
    BO,
    BO_TARGET_BASE,
    CROB_LATCH_OFF,
    CROB_LATCH_ON,
    CROB_PULSE_ON,
)

logger = logging.getLogger(__name__)

__all__ = ["GridLinkBridgeSettings", "ScadaGridLinkBridge", "controls_for"]


@dataclass(frozen=True)
class GridLinkBridgeSettings:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 20001
    master_address: int = 1
    outstation_address: int = 10
    heartbeat_s: float = 5.0
    targets: Mapping[str, int] = field(default_factory=dict)
    tls: Mapping[str, str] = field(default_factory=dict)
    also_mqtt: bool = True

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any] | None) -> GridLinkBridgeSettings:
        raw = dict(raw or {})
        return cls(
            enabled=bool(raw.get("enabled", False)),
            host=str(raw.get("host", cls.host)),
            port=int(raw.get("port", cls.port)),
            master_address=int(raw.get("master_address", cls.master_address)),
            outstation_address=int(raw.get("outstation_address", cls.outstation_address)),
            heartbeat_s=float(raw.get("heartbeat_s", cls.heartbeat_s)),
            targets={str(k): int(v) for k, v in dict(raw.get("targets") or {}).items()},
            tls={str(k): str(v) for k, v in dict(raw.get("tls") or {}).items()},
            also_mqtt=bool(raw.get("also_mqtt", True)),
        )

    def master(self) -> Dnp3Master:
        context = None
        if self.tls:
            context = client_ssl_context(self.tls["ca_file"], self.tls["cert_file"], self.tls["key_file"])
        return Dnp3Master(
            self.host,
            self.port,
            master_address=self.master_address,
            outstation_address=self.outstation_address,
            ssl_context=context,
            server_hostname=self.tls.get("server_hostname"),
        )


Control = tuple[str, int, int]  # ("AO" | "CROB", index, value or CROB code)


def controls_for(message: Mapping[str, Any], targets: Mapping[str, int]) -> list[Control]:
    """The DNP3 controls that express one instruction message (module docstring). Pure."""
    position = targets.get(str(message.get("bank_id")))
    if position is None:
        return []
    lifted = message.get("expires_at") is not None and message.get("expires_at") == message.get("issued_at")
    kind = message.get("kind")
    limit_crob, block_crob = BO_TARGET_BASE + 2 * position, BO_TARGET_BASE + 2 * position + 1
    if kind == "LIMIT":
        if lifted:
            return [("CROB", limit_crob, CROB_LATCH_OFF)]
        limit_kw = int(round(float(message.get("limit_kw") or 0.0)))
        return [("AO", AO_TARGET_BASE + position, limit_kw), ("CROB", limit_crob, CROB_LATCH_ON)]
    if kind == "BLOCK":
        return [("CROB", block_crob, CROB_LATCH_OFF if lifted else CROB_LATCH_ON)]
    return []


class ScadaGridLinkBridge:
    """Sends instruction messages and a heartbeat over one DNP3 association; reconnects on failure."""

    def __init__(self, settings: GridLinkBridgeSettings, master: Dnp3Master | None = None) -> None:
        self.settings = settings
        self._master = master or settings.master()
        self._queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue()
        self.sent: list[tuple[Control, int]] = []  # (control, status) for the control-plane log

    def submit(self, instructions: list[tuple[str, dict[str, Any]]]) -> None:
        for _topic, message in instructions:
            if message.get("kind") == "ESTOP":
                logger.info("ESTOP is not a grid-link point; it stays on MQTT")
                continue
            self._queue.put_nowait(message)

    async def send(self, message: Mapping[str, Any]) -> list[int]:
        statuses: list[int] = []
        for kind, index, value in controls_for(message, self.settings.targets):
            if kind == "AO":
                status = await self._master.analog_output(index, value)
            else:
                status = await self._master.crob(index, value, sbo=True)
            self.sent.append(((kind, index, value), status))
            statuses.append(status)
        return statuses

    async def heartbeat(self) -> int:
        return await self._master.crob(BO["HEARTBEAT"], CROB_PULSE_ON)

    async def run(self) -> None:
        """Connect, heartbeat every `heartbeat_s`, and send queued instructions. Never returns."""
        backoff = 1.0
        retry: Mapping[str, Any] | None = None  # a message whose send failed is re-sent first
        while True:
            try:
                if not self._master.connected:
                    await self._master.connect()
                    backoff = 1.0
                await self.heartbeat()
                if retry is not None:
                    await self.send(retry)
                    retry = None
                deadline = asyncio.get_running_loop().time() + self.settings.heartbeat_s
                while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
                    try:
                        retry = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                    except TimeoutError:
                        break
                    await self.send(retry)
                    retry = None
            except Dnp3MasterError as exc:
                logger.warning("scada grid link down; retrying: %s", exc)
                await self._master.close()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
