"""MQTT I/O for `og-guardian` (02b S6.1-6.2, `interfaces/mqtt/topics.md`).

Two directions:
1. **Independent telemetry read** (BUILD.md: "It reads its own hub telemetry, subscribe `<root>/tel/#`").
   `MqttHubStatePort` is guardian's OWN telemetry cache, filled by `run_telemetry_listener` -- it never
   reads `og.hub_state` (the row `opengrid.fleet`/`engine` maintain), because K1/K4's guarantee depends
   on guardian checking a value nobody else could have altered on the way in.
2. **Signed publish**: `publish_command_batch` (`<root>/cmd/<bank_id>/batch`, QoS 1) and
   `publish_lease` (`<root>/lease/<hub_id>`, QoS 1, retained) -- guardian is the only publisher of both.
"""

from __future__ import annotations

import json
import logging

import aiomqtt

from opengrid.core.models.mqtt import CommandBatch, Lease, Telemetry
from opengrid.guardian.ports import HubSnapshot
from opengrid.platform.config import Config
from opengrid.platform.mqtt import topic, validate_payload

logger = logging.getLogger(__name__)


class MqttHubStatePort:
    """Guardian's own hub-state view, built exclusively from telemetry it has itself received over
    MQTT plus each hub's static physical parameters (capacity/reserve/power limit are configuration,
    not a live signal, so reading them from `og.hub` does not compromise independence)."""

    def __init__(self, hub_params_by_id: dict[str, HubSnapshot]) -> None:
        # hub_params_by_id is seeded with soc/p_kw placeholders; ingest() overwrites them per message.
        self._snapshots = dict(hub_params_by_id)

    def ingest(self, message: Telemetry) -> None:
        prior = self._snapshots.get(message.hub_id)
        if prior is None:
            return  # unknown hub (not in og.hub) -- G-01/G-02 will fail closed as HUB_UNKNOWN downstream
        self._snapshots[message.hub_id] = HubSnapshot(
            params=prior.params, soc_kwh=message.soc_kwh, prev_p_kw=message.p_kw, health=message.health
        )

    async def snapshot(self, hub_id: str) -> HubSnapshot | None:
        return self._snapshots.get(hub_id)


async def run_telemetry_listener(
    cfg: Config, cache: MqttHubStatePort, *, username: str, password: str
) -> None:
    """Subscribe to `<root>/tel/#` for the process lifetime, feeding `cache.ingest`. Runs as a background
    task from `main.py`; a malformed message is logged and skipped, never fatal (K7)."""
    from opengrid.platform.mqtt import build_client

    async with build_client(cfg, username=username, password=password, client_id="og-guardian-tel") as client:
        await client.subscribe(topic(cfg, "tel/#"))
        async for message in client.messages:
            payload = message.payload
            if not isinstance(payload, bytes | bytearray):
                continue
            try:
                data = json.loads(payload)
                validate_payload("telemetry", data)
                cache.ingest(Telemetry.model_validate(data))
            except Exception:
                logger.exception("dropping malformed telemetry message", extra={"topic": str(message.topic)})


async def publish_command_batch(client: aiomqtt.Client, cfg: Config, batch: CommandBatch) -> None:
    """Publish the guardian-signed batch to `<root>/cmd/<bank_id>/batch` (QoS 1, not retained)."""
    validate_payload("command_batch", batch.model_dump(mode="json"))
    await client.publish(
        topic(cfg, f"cmd/{batch.bank_id}/batch"),
        payload=batch.model_dump_json().encode("utf-8"),
        qos=1,
        retain=False,
    )


async def publish_lease(client: aiomqtt.Client, cfg: Config, lease: Lease) -> None:
    """Publish/renew the retained lease for `hub_id` on `<root>/lease/<hub_id>` (QoS 1, retained)."""
    validate_payload("lease", lease.model_dump(mode="json"))
    await client.publish(
        topic(cfg, f"lease/{lease.hub_id}"),
        payload=lease.model_dump_json().encode("utf-8"),
        qos=1,
        retain=True,
    )
