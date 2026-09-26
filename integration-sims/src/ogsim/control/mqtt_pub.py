"""Publishes anomaly control messages to MQTT for the fleet/SCADA simulators
and (optionally) waits for an ack.

Message shape (until interfaces/mqtt/ schemas exist, per BUILD.md):
    {id, target, type, params, start, duration}

Topic: `<root>/scenario/cmd` (publish), `<root>/scenario/ack` (subscribe).
User `og_simctl`, password from env `OG_MQTT_SIMCTL_PASSWORD`. Root from env
`OG_MQTT_ROOT`; the production root `og/v1` is used only by an explicitly marked production
process (`OGSIM_ENV=prod`), exactly as `ogsim.common.config.resolve_topic_root` decides for the fleet
and SCADA sims -- never as a silent fallback.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import aiomqtt

from ogsim.common.config import resolve_topic_root
from ogsim.control.schema_validation import validate_scenario_cmd

DEFAULT_MQTT_PORT = 1883


def mqtt_settings() -> dict[str, Any]:
    return {
        "hostname": os.environ.get("OG_MQTT_HOST", "127.0.0.1"),
        "port": int(os.environ.get("OG_MQTT_PORT", str(DEFAULT_MQTT_PORT))),
        "username": "og_simctl",
        "password": os.environ.get("OG_MQTT_SIMCTL_PASSWORD", ""),
    }


def topic_root() -> str:
    """OG_MQTT_ROOT, else the production root only when marked production (raises otherwise)."""
    return resolve_topic_root({})


async def publish_scenario_cmd(message: dict[str, Any], wait_ack_s: float = 0.0) -> dict[str, Any] | None:
    """Publishes the anomaly control message and, if wait_ack_s > 0, waits
    that long for a matching ack (by id) on `<root>/scenario/ack`."""
    validate_scenario_cmd(message)
    settings = mqtt_settings()
    cmd_topic = f"{topic_root()}/scenario/cmd"
    ack_topic = f"{topic_root()}/scenario/ack"
    payload = json.dumps(message).encode("utf-8")

    async with aiomqtt.Client(**settings) as client:
        if wait_ack_s > 0:
            await client.subscribe(ack_topic)
            await client.publish(cmd_topic, payload=payload, qos=1)
            try:
                async with asyncio.timeout(wait_ack_s):
                    async for msg in client.messages:
                        try:
                            data = json.loads(msg.payload)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if data.get("id") == message.get("id"):
                            return data
            except TimeoutError:
                return None
        else:
            await client.publish(cmd_topic, payload=payload, qos=1)
            return None
    return None
