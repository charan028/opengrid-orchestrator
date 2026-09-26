"""ogsim.customer CLI entry point: `python -m ogsim.customer`."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ogsim.common.clock import RealClock
from ogsim.common.mqtt_client import AiomqttTransportAdapter, SimMqttClient, mqtt_settings
from ogsim.customer.api_client import CustomerApiClient, HttpxTransport
from ogsim.customer.config import API_BASE_URL_ENV_VAR, load_customer_config, resolve_customer_credentials
from ogsim.customer.runtime import CustomerEngine, discover_contract_ids, run_customer

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}'
    )


def _dispatch_message(engine: CustomerEngine, topic: str, payload: Any) -> None:
    if not topic.endswith("/scenario/cmd"):
        return
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        logger.warning("dropping unparseable scenario/cmd message")
        return
    engine.handle_scenario_cmd(data)


def _build_api_clients(config: Any, timeout_s: float) -> dict[str, CustomerApiClient]:
    """One `CustomerApiClient` per Apache basic-auth group actually used by `config.sites`,
    authenticating through Apache (never the loopback API, never a self-set
    X-Remote-User -- lead coordination). Credentials are read once at startup and never
    logged.

    Refuses to build any client -- returning `{}`, which `run_customer` treats as "API part
    disabled, MQTT signals still run" -- if `config.api_base_url` is unset. The orchestrator's
    loopback port now 401s without the Apache proxy secret, so calling it directly (or
    guessing a default) is worse than not calling the API at all."""
    if not config.api_base_url:
        logger.warning(
            "%s is not set; refusing to start the customer API part (MQTT signals still run)",
            API_BASE_URL_ENV_VAR,
        )
        return {}
    transport = HttpxTransport(config.api_base_url, timeout_s=timeout_s)
    groups = {site.resolved_auth_group() for site in config.sites} - {""}
    clients: dict[str, CustomerApiClient] = {}
    for group in groups:
        auth = resolve_customer_credentials(group)
        clients[group] = CustomerApiClient(transport, auth, mode=config.api_mode)
    return clients


async def _run_forever() -> None:
    import aiomqtt

    config = load_customer_config()
    engine = CustomerEngine(config)
    apis = _build_api_clients(config, config.api_timeout_s)
    clock = RealClock()

    async with aiomqtt.Client(
        **mqtt_settings(config.mqtt.host, config.mqtt.port, config.mqtt.password, config.mqtt.username)
    ) as raw:
        client = SimMqttClient(AiomqttTransportAdapter(raw), config.mqtt.topic_root)
        await client.subscribe("scenario/cmd", qos=1)
        await discover_contract_ids(apis, engine)

        async def consume() -> None:
            async for msg in client.messages():
                _dispatch_message(engine, str(msg.topic), msg.payload)

        await asyncio.gather(consume(), run_customer(client, apis, engine, clock))


def main() -> None:
    _configure_logging()
    asyncio.run(_run_forever())


if __name__ == "__main__":
    main()
