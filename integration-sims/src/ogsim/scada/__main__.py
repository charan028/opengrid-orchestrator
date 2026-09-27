"""ogsim.scada CLI entry point: `python -m ogsim.scada`."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ogsim.common.clock import RealClock
from ogsim.common.config import load_scada_config
from ogsim.common.mqtt_client import AiomqttTransportAdapter, SimMqttClient, mqtt_settings
from ogsim.scada.grid_link import GridLinkBridgeSettings, ScadaGridLinkBridge
from ogsim.scada.runtime import ScadaEngine, run_scada

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}'
    )


def _dispatch_message(engine: ScadaEngine, topic: str, payload: Any) -> None:
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        logger.warning("dropping unparseable message on %s", topic)
        return
    if "/tel/" in topic:
        engine.ingest_telemetry(data["hub_id"], data["bank_id"], data["p_kw"])
    elif topic.endswith("/scenario/cmd"):
        engine.handle_scenario_cmd(data)


async def _run_forever() -> None:
    import aiomqtt

    config = load_scada_config()
    engine = ScadaEngine(config)
    clock = RealClock()

    async with aiomqtt.Client(
        **mqtt_settings(config.mqtt.host, config.mqtt.port, config.mqtt.password, config.mqtt.username)
    ) as raw:
        client = SimMqttClient(AiomqttTransportAdapter(raw), config.mqtt.topic_root)

        async def consume() -> None:
            async for msg in client.messages():
                _dispatch_message(engine, str(msg.topic), msg.payload)

        bridge_settings = GridLinkBridgeSettings.from_raw(config.grid_link)
        bridge = ScadaGridLinkBridge(bridge_settings) if bridge_settings.enabled else None
        tasks = [consume(), run_scada(client, engine, clock, bridge)]
        if bridge is not None:
            tasks.append(bridge.run())
        await asyncio.gather(*tasks)


def main() -> None:
    _configure_logging()
    asyncio.run(_run_forever())


if __name__ == "__main__":
    main()
