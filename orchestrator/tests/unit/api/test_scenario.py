"""Scenario panel (02b S5.5/S7.1): validated MQTT publish to `og/v1/scenario/cmd`. The MQTT client
itself is faked -- no broker in unit tests (BUILD.md S5)."""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import OPERATOR_HEADERS, VIEWER_HEADERS


class _FakeMqttClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def __aenter__(self) -> _FakeMqttClient:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def publish(self, topic: str, payload: bytes, qos: int) -> None:
        self.published.append((topic, payload))


@pytest.fixture
def _fake_mqtt(monkeypatch) -> _FakeMqttClient:
    fake_client = _FakeMqttClient()
    monkeypatch.setattr("opengrid.api.routers.scenario.build_client", lambda *a, **k: fake_client)
    return fake_client


def test_trigger_scenario_publishes_and_traces(client, _fake_mqtt, fake_store) -> None:
    resp = client.post(
        "/og/api/scenario/MARKET_PRICE_SPIKE",
        headers=OPERATOR_HEADERS,
        json={"target_kind": "sim", "target_ref": "fleet", "params": {"price_usd_per_mwh": 5000}},
    )
    assert resp.status_code == 200
    assert len(_fake_mqtt.published) == 1
    topic, _payload = _fake_mqtt.published[0]
    assert topic.endswith("scenario/cmd")
    assert fake_store.operator_actions[-1]["action_kind"] == "MANUAL_COMMAND"


def test_trigger_scenario_requires_operator(client, _fake_mqtt) -> None:
    resp = client.post("/og/api/scenario/MARKET_PRICE_SPIKE", headers=VIEWER_HEADERS, json={})
    assert resp.status_code == 403


def test_trigger_unknown_scenario_type_is_422(client, _fake_mqtt) -> None:
    resp = client.post("/og/api/scenario/NOT_A_REAL_SCENARIO", headers=OPERATOR_HEADERS, json={})
    assert resp.status_code == 422
