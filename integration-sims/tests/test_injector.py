"""Tests for ogsim.control.injector.Injector: dispatch routing, the shared
active-anomaly registry, and the JSONL log. Network calls are stubbed by the
autouse `_stub_network_calls` fixture in conftest.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ogsim.control.injector import Injector, UnknownAnomalyTypeError
from ogsim.control.log import AnomalyLog


@pytest.fixture
def injector(tmp_path: Path) -> Injector:
    return Injector(log=AnomalyLog(str(tmp_path / "anomalies.jsonl")))


async def test_inject_unknown_type_raises(injector: Injector):
    with pytest.raises(UnknownAnomalyTypeError):
        await injector.inject(type_="not_a_type", target="*")


async def test_inject_market_type_calls_market_client(
    injector: Injector, _stub_network_calls: list[dict[str, Any]]
):
    await injector.inject(type_="price_spike", target="np6-905-cd", params={"value_usd_per_mwh": 5000})
    kinds = [c["kind"] for c in _stub_network_calls]
    assert "market_inject" in kinds


async def test_inject_scada_type_publishes_to_mqtt(
    injector: Injector, _stub_network_calls: list[dict[str, Any]]
):
    await injector.inject(type_="bank_overload", target="BANK_01")
    kinds = [c["kind"] for c in _stub_network_calls]
    assert "mqtt_publish" in kinds


async def test_mqtt_message_conforms_to_scenario_control_shape(
    injector: Injector, _stub_network_calls: list[dict[str, Any]]
):
    """target is {kind, ref}, type is the wire enum value (not the catalogue
    id), start is an ISO-8601 string, and duration_s is an int - per
    interfaces/mqtt/scenario_control.schema.json."""
    await injector.inject(type_="bank_overload", target="BANK_07", duration=120.0)
    message = next(c["message"] for c in _stub_network_calls if c["kind"] == "mqtt_publish")
    assert message["target"] == {"kind": "bank", "ref": "BANK_07"}
    assert message["type"] == "SCADA_BANK_OVERLOAD"
    assert message["duration_s"] == 120
    assert "T" in message["start"]  # ISO-8601 date-time, not an epoch float


async def test_cancel_of_a_fleet_anomaly_republishes_with_zero_duration(
    injector: Injector, _stub_network_calls: list[dict[str, Any]]
):
    record = await injector.inject(type_="hub_offline", target="HUB_1", duration=300.0)
    _stub_network_calls.clear()
    await injector.cancel(record.id)
    message = next(c["message"] for c in _stub_network_calls if c["kind"] == "mqtt_publish")
    assert message["type"] == "FLEET_HUB_OFFLINE"
    assert message["duration_s"] == 0


async def test_inject_records_source_and_owner(injector: Injector):
    record = await injector.inject(type_="hub_offline", target="HUB_1", source="scenario")
    assert record.owner == "fleet"
    assert record.source == "scenario"
    assert record.end == record.start + record.duration


async def test_injected_anomaly_appears_in_active_list(injector: Injector):
    record = await injector.inject(type_="price_spike", target="*", duration=120.0)
    active = injector.active()
    assert record.id in {a.id for a in active}


async def test_active_can_be_filtered_by_source(injector: Injector):
    await injector.inject(type_="price_spike", target="*", duration=120.0, source="manual")
    await injector.inject(type_="negative_price", target="*", duration=120.0, source="random")
    assert len(injector.active(source="manual")) == 1
    assert len(injector.active(source="random")) == 1


async def test_cancel_removes_from_active_and_returns_true(injector: Injector):
    record = await injector.inject(type_="price_spike", target="*", duration=120.0)
    assert await injector.cancel(record.id) is True
    assert record.id not in {a.id for a in injector.active()}


async def test_cancel_unknown_id_returns_false(injector: Injector):
    assert await injector.cancel("no-such-id") is False


async def test_injection_log_records_inject_and_cancel_actions(injector: Injector):
    record = await injector.inject(type_="price_spike", target="*", duration=60.0)
    await injector.cancel(record.id)
    log = injector.injection_log()
    actions = [row["action"] for row in log]
    assert actions.count("inject") == 1
    assert actions.count("cancel") == 1


async def test_injection_log_entries_include_end_time(injector: Injector):
    record = await injector.inject(type_="price_spike", target="*", duration=60.0)
    log = injector.injection_log()
    inject_entry = next(row for row in log if row["action"] == "inject")
    assert inject_entry["end"] == pytest.approx(record.start + 60.0)
