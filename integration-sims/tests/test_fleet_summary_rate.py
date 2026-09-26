"""Waveform summary publish rate (wave-2 rate fix): each hub's summary is gated to
`wave_summary_interval_s` (default 10 s) instead of every 2 s telemetry tick, so 2,000 hubs publish
~200 summaries/s, not ~1,000."""

from __future__ import annotations

from dataclasses import replace

from ogsim.common.config import MqttSettings, load_fleet_config
from ogsim.fleet.runtime import FleetEngine

MQTT = MqttSettings(host="localhost", port=1883, username="u", password="p", topic_root="ogtest/rate")


def _engine() -> FleetEngine:
    config = replace(
        load_fleet_config(),
        mqtt=MQTT,
        hub_count=8,
        bank_count=2,
        zones=("LZ_NORTH", "LZ_SOUTH"),
        wave_summary_interval_s=10.0,
        wave_summary_delta_pct=1000.0,  # isolate the time gate from the on-change trigger
    )
    return FleetEngine(config, seed=7)


def test_each_hub_publishes_one_summary_per_interval_not_per_tick() -> None:
    engine = _engine()
    # Not a hardcoded 8: the shipped fleet.yaml's substation asset (D-29(b)) adds one more hub on top
    # of this fixture's own hub_count=8 override (substation_assets isn't itself overridden above).
    expected_hub_count = len(engine.state.hub_ids)

    first = engine.wave_summary_messages(1000.0)
    assert len(first) == expected_hub_count
    assert engine.wave_summary_messages(1002.0) == []  # next 2 s tick: nothing due
    assert len(engine.wave_summary_messages(1010.0)) == expected_hub_count
