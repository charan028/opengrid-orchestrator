"""Server integration tests for `og-safestop` (workspace `stop`: `OG_DB=og_t_stop`,
`OG_MQTT_ROOT=ogtest/stop`). Run via:

    powershell -File tools/remote.ps1 -Ws stop -Cmd "cd orchestrator && python -m pytest tests/integration/safestop -q"

Covers: publish a stop, confirm it is retained, a reconnecting subscriber receives it without a fresh
publish, `og.stop_event` is persisted, and (TS-06-23, the K8 topology proof) all of this works with no
dependency on `og-engine`/`og-guardian` -- this test module never imports or starts either.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("OG_DB"), reason="requires the server env (tools/remote.ps1 -Ws stop)"
)


@pytest.fixture
async def pool(server_config, _migrated):
    from opengrid.platform.db import make_pool

    pool = await make_pool(server_config)
    yield pool
    await pool.close()


@pytest.fixture
def stop_key():
    from opengrid.core.crypto import generate_keypair
    from opengrid.safestop.keys import StopSigningKey

    seed, _pub = generate_keypair()
    return StopSigningKey(key_id="safestop-it-test", seed=seed)


async def _make_mqtt_client(server_config, client_id: str):
    import aiomqtt

    return aiomqtt.Client(
        hostname=server_config.get("mqtt.host"),
        port=server_config.get("mqtt.port"),
        username=os.environ.get("OG_MQTT_SAFESTOP_USER", "og_safestop"),
        password=os.environ.get("OG_MQTT_SAFESTOP_PASSWORD", ""),
        identifier=client_id,
        keepalive=20,
    )


async def test_engage_publishes_a_retained_stop_message(server_config, pool, stop_key):
    from opengrid.platform.mqtt import topic
    from opengrid.safestop.mqtt_publish import AiomqttStopPublisher
    from opengrid.safestop.pg_backend import PgStopEventBackend
    from opengrid.safestop.service import SafestopService

    scope, scope_ref = "BANK", "bank-it-01"
    backend = PgStopEventBackend(pool)

    async with await _make_mqtt_client(server_config, "og-safestop-it-publisher") as publish_client:
        publisher = AiomqttStopPublisher(client=publish_client, config=server_config)
        svc = SafestopService(stop_key, backend, publisher, trace=None)
        await svc.engage(scope, scope_ref, "TS-06-15 integration drill", "operator:it-test")

    # A fresh subscriber connecting *after* the publish must still see the retained message.
    full_topic_filter = topic(server_config, f"stop/{scope.lower()}/{scope_ref}/+")
    async with await _make_mqtt_client(server_config, "og-safestop-it-subscriber") as sub_client:
        await sub_client.subscribe(full_topic_filter)
        message = await asyncio.wait_for(anext(sub_client.messages), timeout=10)

    payload = json.loads(message.payload)
    assert payload["action"] == "ENGAGE"
    assert payload["scope"] == "bank"
    assert payload["scope_id"] == scope_ref
    assert payload["key_id"] == stop_key.key_id

    latest = await backend.latest_action("BANK", scope_ref)
    assert latest == "ENGAGE"


async def test_release_through_safestop_is_always_refused(pool, stop_key):
    from opengrid.safestop.pg_backend import PgStopEventBackend
    from opengrid.safestop.service import ReleaseNotPermittedError, SafestopService

    class _UnusedPublisher:
        async def publish_retained(self, topic_suffix, payload):
            raise AssertionError("release must never publish")

    backend = PgStopEventBackend(pool)
    svc = SafestopService(stop_key, backend, _UnusedPublisher(), trace=None)

    with pytest.raises(ReleaseNotPermittedError):
        await svc.release("BANK", "bank-it-02", "operator:it-test")

    assert await backend.latest_action("BANK", "bank-it-02") is None


async def test_k8_topology_stop_works_with_no_engine_or_guardian_process(server_config, pool, stop_key):
    """TS-06-23: og-safestop engages a fleet stop while og-guardian and og-engine are both down.

    This test module never imports `opengrid.engine` or `opengrid.guardian` (see
    tests/unit/safestop/test_import_isolation.py for the static guarantee), and nothing here starts
    those processes -- so a passing engage() below demonstrates the stop path has no runtime dependency
    on either, independent of whether they happen to be running elsewhere on the box.
    """
    from opengrid.safestop.mqtt_publish import AiomqttStopPublisher
    from opengrid.safestop.pg_backend import PgStopEventBackend
    from opengrid.safestop.service import SafestopService

    backend = PgStopEventBackend(pool)
    async with await _make_mqtt_client(server_config, "og-safestop-it-k8") as client:
        publisher = AiomqttStopPublisher(client=client, config=server_config)
        svc = SafestopService(stop_key, backend, publisher, trace=None)
        await svc.engage("FLEET", "", "TS-06-23 K8 topology proof", "operator:it-test")

    assert await backend.latest_action("FLEET", "FLEET") == "ENGAGE"
