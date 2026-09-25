"""Integration test against a real Mosquitto broker (server-only).

Collected everywhere, but skips gracefully within a short timeout when no
broker is reachable at OG_MQTT_HOST:OG_MQTT_PORT (i.e. everywhere except
the actual server), per BUILD.md's ask for one integration test file that
does not require mocking the MQTT boundary.
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytest.importorskip("aiomqtt")

import aiomqtt  # noqa: E402

from ogsim.common.config import load_fleet_config  # noqa: E402
from ogsim.common.mqtt_client import AiomqttTransportAdapter, SimMqttClient  # noqa: E402

CONNECT_TIMEOUT_S = 1.5


async def _broker_reachable(host: str, port: int, username: str, password: str) -> bool:
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT_S):
            async with aiomqtt.Client(hostname=host, port=port, username=username, password=password):
                return True
    except (TimeoutError, aiomqtt.MqttError, OSError):
        return False


def test_fleet_can_publish_telemetry_to_a_live_broker() -> None:
    config = load_fleet_config()
    reachable = asyncio.run(
        _broker_reachable(config.mqtt.host, config.mqtt.port, config.mqtt.username, config.mqtt.password)
    )
    if not reachable:
        pytest.skip(
            f"no MQTT broker reachable at {config.mqtt.host}:{config.mqtt.port} (expected off-server)"
        )

    async def scenario() -> None:
        async with aiomqtt.Client(
            hostname=config.mqtt.host,
            port=config.mqtt.port,
            username=config.mqtt.username,
            password=config.mqtt.password,
        ) as raw:
            client = SimMqttClient(AiomqttTransportAdapter(raw), os.environ.get("OG_MQTT_ROOT", "og/v1"))
            await client.publish_validated(
                "telemetry",
                "tel/LZ_NORTH/bank-000/hub-00000",
                {
                    "hub_id": "hub-00000",
                    "bank_id": "bank-000",
                    "zone": "LZ_NORTH",
                    "ts": "2026-09-26T18:00:00.000Z",
                    "soc_kwh": 5.0,
                    "p_kw": 0.0,
                    "health": "online",
                    "seq": 1,
                    "epoch": 1,
                    "fault_code": None,
                },
            )

    asyncio.run(scenario())
