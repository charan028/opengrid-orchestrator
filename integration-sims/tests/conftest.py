"""Shared test fixtures. Every test that touches `ogsim.control.injector`
gets network calls (market's admin HTTP API, MQTT) stubbed out, so the suite
never depends on a running market server or MQTT broker."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from ogsim.control import market_client, mqtt_pub


@pytest.fixture(autouse=True)
def _never_production_mqtt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests are never the production sim: no production marker, and a non-production topic root
    unless the environment (tools/remote.ps1's OG_MQTT_ROOT) already provides one. Without this, the
    shipped YAML's `og/v1` root is (correctly) refused by `ogsim.common.config`."""
    monkeypatch.delenv("OGSIM_ENV", raising=False)
    if not os.environ.get("OG_MQTT_ROOT"):
        monkeypatch.setenv("OG_MQTT_ROOT", "ogtest/unit")


@pytest.fixture(autouse=True)
def _isolate_anomaly_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets its own JSONL log path by default, so nothing ever
    writes to a real /var/lib/opengrid/sim or repo-root anomalies.jsonl."""
    monkeypatch.setenv("OGSIM_ANOMALY_LOG_PATH", str(tmp_path / "anomalies.jsonl"))


@pytest.fixture(autouse=True)
def _stub_network_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_market_inject(anomaly: dict[str, Any], base_url: str | None = None) -> dict[str, Any]:
        calls.append({"kind": "market_inject", "anomaly": anomaly})
        return {"ok": True, "anomaly": anomaly}

    async def fake_market_cancel(anomaly_id: str, base_url: str | None = None) -> dict[str, Any]:
        calls.append({"kind": "market_cancel", "id": anomaly_id})
        return {"ok": True}

    async def fake_publish_scenario_cmd(message: dict[str, Any], wait_ack_s: float = 0.0) -> None:
        calls.append({"kind": "mqtt_publish", "message": message})
        return None

    monkeypatch.setattr(market_client, "inject", fake_market_inject)
    monkeypatch.setattr(market_client, "cancel", fake_market_cancel)
    monkeypatch.setattr(mqtt_pub, "publish_scenario_cmd", fake_publish_scenario_cmd)
    return calls
