"""Tests that the anomaly catalogue matches BUILD.md §3's minimum set."""

from __future__ import annotations

from ogsim.control import catalogue

REQUIRED_SCADA_TYPES = {
    "bank_overload",
    "load_spike",
    "frozen_value",
    "bad_quality_flag",
    "stale_no_update",
    "out_of_range_value",
    "oscillation",
    "phase_imbalance",
    "breaker_open",
    "comms_loss",
    "utility_instruction",
    "time_skew",
}
REQUIRED_FLEET_TYPES = {
    "hub_offline",
    "zone_mass_disconnect",
    "not_following_commands",
    "inverter_trip",
    "soc_sensor_drift",
    "telemetry_delay_burst",
    "lease_loss",
    "clock_skew",
    "tampered_unsigned_command",
    "reserve_floor_pressure",
}
REQUIRED_MARKET_TYPES = {
    "price_spike",
    "negative_price",
    "as_price_jump",
    "http_5xx",
    "http_429",
    "http_401_primary",
    "stale_posting",
    "malformed_payload",
    "slow_response",
    "nws_extreme_weather",
}


def test_catalogue_covers_every_required_scada_type():
    assert REQUIRED_SCADA_TYPES <= catalogue.SCADA_TYPES


def test_catalogue_covers_every_required_fleet_type():
    assert REQUIRED_FLEET_TYPES <= catalogue.FLEET_TYPES


def test_catalogue_covers_every_required_market_type():
    assert REQUIRED_MARKET_TYPES <= catalogue.MARKET_TYPES


def test_every_catalogue_entry_has_a_description():
    for entry in catalogue.CATALOGUE:
        assert entry.description


def test_owner_of_returns_none_for_unknown_type():
    assert catalogue.owner_of("not-a-real-anomaly") is None


def test_owner_of_returns_the_correct_owner():
    assert catalogue.owner_of("price_spike") == "market"
    assert catalogue.owner_of("bank_overload") == "scada"
    assert catalogue.owner_of("hub_offline") == "fleet"


def test_as_list_round_trips_every_catalogue_entry():
    listed_ids = {entry["id"] for entry in catalogue.as_list()}
    assert listed_ids == {a.id for a in catalogue.CATALOGUE}
