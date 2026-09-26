"""`opengrid.guardian.mqtt_io`: the independent telemetry cache and signed-batch/lease publishing,
against a fake `aiomqtt.Client` (no real broker -- BUILD.md S5 "Local: ... no DB/MQTT")."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from opengrid.core.models.mqtt import CommandBatch, CommandItem, Lease, ScadaUtilityInstruction, Telemetry
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

    def fake_build_client(cfg, *, username, password, process):
        return fake_client

    import opengrid.platform.mqtt as platform_mqtt

    monkeypatch.setattr(platform_mqtt, "build_client", fake_build_client)

    await mqtt_io.run_telemetry_listener(_cfg(), cache, username="og_guardian", password="x")

    snap = await cache.snapshot("hub-1")
    assert snap is not None and snap.soc_kwh == 12.0


def _seed(*hub_ids: str) -> dict[str, HubSnapshot]:
    return {
        hub_id: HubSnapshot(
            params=HubParams(e_kwh=39.2, r_kwh=7.84, p_kw=11.0), soc_kwh=0.0, prev_p_kw=0.0, health="stale"
        )
        for hub_id in hub_ids
    }


def _telemetry(hub_id: str, *, health: str = "online") -> Telemetry:
    return Telemetry(
        hub_id=hub_id,
        bank_id="bank-1",
        zone="z",
        ts=NOW,
        soc_kwh=20.0,
        p_kw=-3.0,
        health=health,
        seq=1,
        epoch=1,
    )


async def test_hub_whose_telemetry_stopped_is_reported_stale():
    """K1: before, the cache kept a silent hub "online" on its last SoC forever, so the guardian kept
    signing discharge on an arbitrarily old reading. Beyond `max_age_s` it is stale (zero discharge)."""
    clock = {"t": 100.0}
    cache = mqtt_io.MqttHubStatePort(_seed("hub-1"), max_age_s=60.0, monotonic_fn=lambda: clock["t"])
    cache.ingest(_telemetry("hub-1"))

    clock["t"] = 159.0
    fresh = await cache.snapshot("hub-1")
    assert fresh is not None and fresh.health == "online"

    clock["t"] = 161.0
    silent = await cache.snapshot("hub-1")
    assert silent is not None and silent.health == "stale" and silent.soc_kwh == 20.0


async def test_member_snapshots_cover_every_configured_hub_of_the_bank():
    cache = mqtt_io.MqttHubStatePort(
        _seed("hub-1", "hub-2", "hub-3"),
        bank_by_hub={"hub-1": "bank-1", "hub-2": "bank-1", "hub-3": "bank-2"},
        max_age_s=60.0,
        monotonic_fn=lambda: 0.0,
    )
    cache.ingest(_telemetry("hub-1"))

    members = await cache.member_snapshots("bank-1")

    assert sorted(m.health for m in members) == ["online", "stale"]  # hub-2 never reported
    assert await cache.member_snapshots("bank-unknown") == []


def _instruction(**overrides: object) -> ScadaUtilityInstruction:
    data: dict[str, object] = {
        "instruction_id": "019842d1-0000-7000-8000-0000000000aa",
        "bank_id": "bank-1",
        "kind": "LIMIT",
        "limit_kw": 40.0,
        "issued_at": NOW,
        "expires_at": NOW + timedelta(minutes=5),
        "issued_by": "utility",
    }
    data.update(overrides)
    return ScadaUtilityInstruction.model_validate(data)


async def test_l2_instruction_port_reports_only_unexpired_instructions():
    now = {"t": NOW}
    port = mqtt_io.MqttL2InstructionPort(now_fn=lambda: now["t"])
    assert await port.active_instruction("bank-1") is None

    port.ingest(_instruction())
    active = await port.active_instruction("bank-1")
    assert active is not None and active.kind == "LIMIT" and active.limit_kw == 40.0
    assert await port.active_instruction("bank-2") is None

    now["t"] = NOW + timedelta(minutes=5)
    assert await port.active_instruction("bank-1") is None


async def test_listener_routes_utility_instructions_to_the_guardians_own_l2_port(monkeypatch):
    """K5: the guardian's L2 read is its own subscription (the previous Postgres port read a trace key
    the engine never writes, so G-15 never saw an instruction)."""
    cache = mqtt_io.MqttHubStatePort(_seed("hub-1"))
    l2 = mqtt_io.MqttL2InstructionPort(now_fn=lambda: NOW)
    messages = [
        FakeMessage("ogtest/guard/scada/instruction/bank-1", _instruction().model_dump_json().encode()),
        FakeMessage("ogtest/guard/tel/z/bank-1/hub-1", _telemetry("hub-1").model_dump_json().encode()),
    ]
    fake_client = FakeSubscribeClient(messages)
    import opengrid.platform.mqtt as platform_mqtt

    monkeypatch.setattr(platform_mqtt, "build_client", lambda cfg, **kwargs: fake_client)

    await mqtt_io.run_telemetry_listener(
        _cfg(), cache, username="og_guardian", password="x", l2_instructions=l2
    )

    assert "ogtest/guard/scada/instruction/+" in fake_client.subscribed
    assert await l2.active_instruction("bank-1") is not None
    snap = await cache.snapshot("hub-1")
    assert snap is not None and snap.health == "online"


async def test_publish_calibration_command_goes_to_the_hubs_calibration_topic():
    from uuid import UUID

    from opengrid.core.models.pq import (
        CalibrationBounds,
        CalibrationCommand,
        CalibrationCorrection,
        CalibrationReference,
    )

    client = FakePublishClient()
    command = CalibrationCommand(
        calibration_id=UUID("00000000-0000-4000-8000-0000000000c1"),
        hub_id="hub-1",
        epoch=1,
        seq=1,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
        reference=CalibrationReference(
            phase_deg=0.0, freq_hz=60.0, amplitude_v=240.0, sync_source="ntp_disciplined"
        ),
        correction=CalibrationCorrection(freq_hz=0.01, voltage_pct=-0.5, phase_deg=1.0),
        bounds=CalibrationBounds(max_freq_hz=0.1, max_voltage_pct=2.0, max_phase_deg=5.0),
        key_id="guardian-2026a",
        signature="c2ln",
    )

    await mqtt_io.publish_calibration_command(client, _cfg(), command)

    topic, payload, qos, retain = client.published[0]
    assert topic == "ogtest/guard/cmd/cal/hub-1"
    assert qos == 1 and retain is False
    assert json.loads(payload)["signature"] == "c2ln"


async def test_an_old_fault_report_is_stale_too():
    """An offline/fault report the guardian has not refreshed is as unverifiable as an old online one."""
    clock = {"t": 0.0}
    cache = mqtt_io.MqttHubStatePort(_seed("hub-1"), max_age_s=60.0, monotonic_fn=lambda: clock["t"])
    cache.ingest(_telemetry("hub-1", health="fault"))
    fresh = await cache.snapshot("hub-1")
    assert fresh is not None and fresh.health == "fault"
    clock["t"] = 61.0
    old = await cache.snapshot("hub-1")
    assert old is not None and old.health == "stale"
