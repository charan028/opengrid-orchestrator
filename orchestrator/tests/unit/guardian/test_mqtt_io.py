"""`opengrid.guardian.mqtt_io`: the independent telemetry cache and signed-batch/lease publishing,
against a fake `aiomqtt.Client` (no real broker -- BUILD.md S5 "Local: ... no DB/MQTT")."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from opengrid.core.models.mqtt import CommandBatch, CommandItem, Lease, Telemetry
from opengrid.core.physics import HubParams
from opengrid.guardian import mqtt_io
from opengrid.guardian.ports import HubSnapshot
from opengrid.platform.config import Config

NOW = datetime(2026, 9, 26, 18, 0, 0, tzinfo=UTC)


class FakeTopic:
    def __init__(self, value: str) -> None:
        self.value = value

    def __str__(self) -> str:
        return self.value


class FakeMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = FakeTopic(topic)
        self.payload = payload


class FakePublishClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, int, bool]] = []

    async def publish(self, topic, payload, qos, retain):
        self.published.append((topic, payload, qos, retain))


class FakeSubscribeClient:
    def __init__(self, messages: list[FakeMessage]) -> None:
        self._messages = messages
        self.subscribed: list[str] = []

    async def subscribe(self, topic_filter: str) -> None:
        self.subscribed.append(topic_filter)

    @property
    def messages(self):
        return self._aiter()

    async def _aiter(self):
        for m in self._messages:
            yield m

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _cfg() -> Config:
    return Config({"mqtt": {"topic_root": "ogtest/guard"}})


def test_mqtt_hub_state_port_ingest_updates_known_hub():
    seed = {
        "hub-1": HubSnapshot(
            params=HubParams(e_kwh=39.2, r_kwh=7.84, p_kw=11.0), soc_kwh=0.0, prev_p_kw=0.0, health="stale"
        )
    }
    cache = mqtt_io.MqttHubStatePort(seed)
    telemetry = Telemetry(
        hub_id="hub-1",
        bank_id="bank-1",
        zone="z",
        ts=NOW,
        soc_kwh=15.0,
        p_kw=2.0,
        health="online",
        seq=1,
        epoch=1,
    )
    cache.ingest(telemetry)
    import asyncio

    snap = asyncio.run(cache.snapshot("hub-1"))
    assert snap is not None and snap.soc_kwh == 15.0 and snap.prev_p_kw == 2.0 and snap.health == "online"


async def test_mqtt_hub_state_port_ignores_unknown_hub():
    cache = mqtt_io.MqttHubStatePort({})
    telemetry = Telemetry(
        hub_id="ghost",
        bank_id="bank-1",
        zone="z",
        ts=NOW,
        soc_kwh=15.0,
        p_kw=2.0,
        health="online",
        seq=1,
        epoch=1,
    )
    cache.ingest(telemetry)
    assert await cache.snapshot("ghost") is None


async def test_publish_command_batch():
    client = FakePublishClient()
    batch = CommandBatch(
        batch_id="019842d1-0000-7000-8000-000000000001",
        bank_id="bank-1",
        epoch=1,
        seq=1,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=10),
        items=[CommandItem(hub_id="hub-1", p_kw_setpoint=3.0, reason_code="SELECTOR")],
        key_id="guardian-2026a",
        signature="c2ln",
    )
    await mqtt_io.publish_command_batch(client, _cfg(), batch)
    assert len(client.published) == 1
    topic, payload, qos, retain = client.published[0]
    assert topic == "ogtest/guard/cmd/bank-1/batch"
    assert qos == 1 and retain is False
    assert json.loads(payload)["bank_id"] == "bank-1"


async def test_publish_lease():
    client = FakePublishClient()
    lease = Lease(hub_id="hub-1", epoch=1, expires_at=NOW + timedelta(seconds=30), issued_at=NOW)
    await mqtt_io.publish_lease(client, _cfg(), lease)
    topic, _payload, qos, retain = client.published[0]
    assert topic == "ogtest/guard/lease/hub-1"
    assert qos == 1 and retain is True


async def test_run_telemetry_listener_ingests_valid_and_skips_malformed(monkeypatch):
    seed = {
        "hub-1": HubSnapshot(
            params=HubParams(e_kwh=39.2, r_kwh=7.84, p_kw=11.0), soc_kwh=0.0, prev_p_kw=0.0, health="stale"
        )
    }
    cache = mqtt_io.MqttHubStatePort(seed)

    good = Telemetry(
        hub_id="hub-1",
        bank_id="bank-1",
        zone="z",
        ts=NOW,
        soc_kwh=12.0,
        p_kw=1.5,
        health="online",
        seq=1,
        epoch=1,
    )
    messages = [
        FakeMessage("ogtest/guard/tel/z/bank-1/hub-1", good.model_dump_json().encode("utf-8")),
        FakeMessage("ogtest/guard/tel/z/bank-1/hub-1", b"not json"),
        FakeMessage(
            "ogtest/guard/tel/z/bank-1/hub-1", b""
        ),  # not bytes-like guard is a no-op here; still bytes
    ]
    fake_client = FakeSubscribeClient(messages)

    def fake_build_client(cfg, *, username, password, client_id):
        return fake_client

    import opengrid.platform.mqtt as platform_mqtt

    monkeypatch.setattr(platform_mqtt, "build_client", fake_build_client)

    await mqtt_io.run_telemetry_listener(_cfg(), cache, username="og_guardian", password="x")

    snap = await cache.snapshot("hub-1")
    assert snap is not None and snap.soc_kwh == 12.0
