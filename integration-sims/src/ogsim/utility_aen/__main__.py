"""ogsim.utility_aen entry point: `python -m ogsim.utility_aen` (unit og-sim-utility.service)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from ogsim.common.clock import RealClock
from ogsim.common.mqtt_client import AiomqttTransportAdapter, SimMqttClient, mqtt_settings
from ogsim.utility_aen.channels import Channel, build_channel
from ogsim.utility_aen.config import UtilitySimConfig, load_config
from ogsim.utility_aen.runtime import UtilitySim

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}'
    )


def _channel(config: UtilitySimConfig) -> Channel | None:
    """No channel (and no credential lookup) for a disabled utility."""
    return build_channel(config.channel, config.channel_settings) if config.utility.enabled else None


async def _consume(sim: UtilitySim, config: UtilitySimConfig) -> None:
    """/ogsim/ triggers: `<root>/scenario/cmd`. Without MQTT settings (disabled utility, or
    `mqtt.enabled: false`) there is nothing to consume."""
    if config.mqtt is None:
        return
    import aiomqtt

    settings = config.mqtt
    async with aiomqtt.Client(
        **mqtt_settings(settings.host, settings.port, settings.password, settings.username)
    ) as raw:
        client = SimMqttClient(AiomqttTransportAdapter(raw), settings.topic_root)
        await client.subscribe("scenario/cmd", qos=1)
        async for msg in client.messages():
            if not str(msg.topic).endswith("/scenario/cmd"):
                continue
            try:
                data = json.loads(msg.payload)
            except (json.JSONDecodeError, TypeError):
                logger.warning("dropping unparseable scenario/cmd message")
                continue
            if isinstance(data, dict):
                sim.handle_scenario_cmd(data)


async def _run() -> None:
    config = load_config()
    channel = _channel(config)
    sim = UtilitySim(config, channel, RealClock())
    try:
        await asyncio.gather(sim.run(), _consume(sim, config))
    finally:
        closer = getattr(channel, "aclose", None)
        if closer is not None:
            with contextlib.suppress(Exception):
                await closer()


def main() -> None:
    _configure_logging()
    asyncio.run(_run())


if __name__ == "__main__":
    main()
