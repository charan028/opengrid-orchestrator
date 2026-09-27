"""Grid-link runtime inside og-engine, the process that owns SCADA ingest (grid-link.md S6.3).

`start_grid_link(cfg, pool, trace)` returns `None` when `[grid_link].enabled` is false or no utility is
enabled (the default: nothing listens, nothing is imported beyond config). Otherwise it starts one task
that runs, per enabled utility, the DNP3 listener and the service worker, plus one MQTT bridge
connection (user `og_gridlink`, publish-only on `<root>/scada/instruction/#`). L2 instructions leave
through that bridge onto the EXISTING topics, so og-engine's own ingest loop and the guardian's
independent L2 port both receive them exactly as from the SCADA feed (K5, G-15).

A failure inside the grid link never stops og-engine: the task logs and restarts the failed part with
backoff; a TLS/config error at start is logged and the grid link stays down (visible in health/logs).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from opengrid.core.models.mqtt import ScadaBankSignal, ScadaUtilityInstruction
from opengrid.integrations.grid_link.config import (
    GridLinkSettings,
    UtilityLinkSettings,
    load_grid_link_settings,
)
from opengrid.integrations.grid_link.dnp3_server import Dnp3GridLinkServer
from opengrid.integrations.grid_link.service import GridLinkService
from opengrid.integrations.sinks import MqttBridgeSink

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from opengrid.platform.config import Config
    from opengrid.trace.store import TraceStore

logger = logging.getLogger(__name__)

__all__ = ["BridgeHolder", "start_grid_link"]

MQTT_PROCESS_NAME = "gridlink"
RECONNECT_MIN_S = 1.0
RECONNECT_MAX_S = 30.0


class BridgeHolder:
    """`ScadaSink` that forwards to the current MQTT bridge; raises while disconnected so the service keeps
    the instruction and re-sends it on its next tick."""

    def __init__(self) -> None:
        self.bridge: MqttBridgeSink | None = None
        self.broken = asyncio.Event()

    async def on_bank_signal(self, signal: ScadaBankSignal) -> None:
        await self._bridge().on_bank_signal(signal)

    async def on_utility_instruction(self, instruction: ScadaUtilityInstruction) -> None:
        try:
            await self._bridge().on_utility_instruction(instruction)
        except ConnectionError:
            raise
        except Exception:
            self.broken.set()
            raise

    def _bridge(self) -> MqttBridgeSink:
        if self.bridge is None:
            raise ConnectionError("grid link MQTT bridge is not connected")
        return self.bridge


async def _mqtt_loop(cfg: Config, settings: GridLinkSettings, holder: BridgeHolder) -> None:
    from opengrid.platform.config import resolve_secret
    from opengrid.platform.mqtt import build_client

    backoff = RECONNECT_MIN_S
    while True:
        try:
            password = resolve_secret(settings.mqtt_password_env)
            async with build_client(
                cfg, username=settings.mqtt_username, password=password, process=MQTT_PROCESS_NAME
            ) as client:
                holder.bridge = MqttBridgeSink(client, cfg.mqtt_topic_root)
                holder.broken.clear()
                backoff = RECONNECT_MIN_S
                logger.info("grid link MQTT bridge connected")
                await holder.broken.wait()
        except Exception as exc:
            logger.warning(
                "grid link MQTT bridge down; retrying", extra={"error": str(exc), "retry_s": backoff}
            )
        finally:
            holder.bridge = None
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, RECONNECT_MAX_S)


async def _run(cfg: Config, pool: AsyncConnectionPool, trace: TraceStore, settings: GridLinkSettings) -> None:
    from opengrid.calls import CallLimits, PgCallStore
    from opengrid.integrations.grid_link.calls_port import CoreTollCallPort
    from opengrid.integrations.grid_link.fleet_telemetry import FleetTelemetryPort, banks_of_zone

    holder = BridgeHolder()
    calls = CoreTollCallPort(PgCallStore(pool), trace, CallLimits.from_config(cfg))
    telemetry = FleetTelemetryPort()
    servers: list[Dnp3GridLinkServer] = []
    async with asyncio.TaskGroup() as group:
        group.create_task(_mqtt_loop(cfg, settings, holder))
        for utility in settings.active_utilities():
            service = GridLinkService(
                utility,
                calls=calls,
                telemetry=telemetry,
                trace=trace.append,
                sink=holder,
                banks_of_zone=banks_of_zone,
            )
            server = _server_for(utility, service)
            try:
                await server.start()
            except (OSError, ValueError) as exc:
                logger.error(
                    "grid link could not start; this utility stays down",
                    extra={"utility_id": utility.utility_id, "error": str(exc)},
                )
                continue
            servers.append(server)
            group.create_task(service.run())
        try:
            await asyncio.Event().wait()
        finally:
            for server in servers:
                await server.stop()


def _server_for(utility: UtilityLinkSettings, service: GridLinkService) -> Dnp3GridLinkServer:
    """The transport for utility.protocol. Only DNP3 exists today; an ICCP server would be chosen here
    (grid-link.md S8), and the service would not change."""
    if utility.protocol == "dnp3":
        return Dnp3GridLinkServer(utility, service)
    raise ValueError(f"unsupported grid-link protocol {utility.protocol!r}")


def start_grid_link(cfg: Config, pool: AsyncConnectionPool, trace: TraceStore) -> asyncio.Task[Any] | None:
    """Start the grid link when configured (module docstring); `None` when disabled."""
    try:
        settings = load_grid_link_settings(cfg.get("grid_link"))
    except ValueError as exc:
        logger.error("invalid [grid_link] configuration; grid link disabled", extra={"error": str(exc)})
        return None
    for utility_id in settings.unknown_utilities():
        logger.error("grid link utility id is not in UTILITY_IDS; skipped", extra={"utility_id": utility_id})
    if not settings.active_utilities():
        return None
    task = asyncio.create_task(_run(cfg, pool, trace, settings), name="grid-link")
    task.add_done_callback(_log_exit)
    return task


def _log_exit(task: asyncio.Task[Any]) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        exc = task.exception()
        if exc is not None:
            logger.error("grid link task stopped", exc_info=exc)
