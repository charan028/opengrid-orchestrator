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


def test_infer_wire_target_kind_picks_bank_for_a_bank_ref():
    # not_following_commands' catalogue target_kind is "hub or bank" and its
    # static wire_target_kind default is "hub" -- a bank-shaped ref must
    # still be reported as "bank", not the static default.
    entry = catalogue.BY_ID["not_following_commands"]
    assert catalogue.infer_wire_target_kind(entry, "bank-003") == "bank"


def test_infer_wire_target_kind_picks_hub_for_a_hub_ref():
    entry = catalogue.BY_ID["not_following_commands"]
    assert catalogue.infer_wire_target_kind(entry, "hub-00042") == "hub"


def test_infer_wire_target_kind_picks_zone_for_a_zone_ref():
    entry = catalogue.BY_ID["reserve_floor_pressure"]
    assert catalogue.infer_wire_target_kind(entry, "LZ_NORTH") == "zone"


def test_infer_wire_target_kind_falls_back_to_default_for_an_unrecognized_ref():
    entry = catalogue.BY_ID["not_following_commands"]
    assert catalogue.infer_wire_target_kind(entry, "*") == entry.wire_target_kind


def test_infer_wire_target_kind_never_picks_a_kind_the_entry_disallows():
    # bank_overload only ever targets a bank; a "hub-"-looking ref must not
    # flip it to "hub" since the catalogue entry doesn't allow that kind.
    entry = catalogue.BY_ID["bank_overload"]
    assert catalogue.infer_wire_target_kind(entry, "hub-00001") == "bank"
