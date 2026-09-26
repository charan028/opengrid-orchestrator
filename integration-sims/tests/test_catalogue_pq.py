"""Tests for the PQ additions to ogsim's anomaly catalogue (ogsim.control.catalogue):
frequency_drift, harmonic_injection, phase_imbalance_injection, calibration_drift_correctable,
calibration_drift_hardware, replace_inverter (owner: fleet, applied by ogsim.fleet.pq), and
site_sag_swell (owner: scada, customer-site voltage sag/swell -- applied by ogsim.scada, not
this agent's lane; only its catalogue metadata is this agent's concern, per BUILD.md WP-A/E
ownership and 06-service-profiles-and-power-quality.md §9.2 Agent E's "the PQ additions to the
ogsim anomaly catalogue")."""

from __future__ import annotations

from ogsim.control import catalogue
from ogsim.fleet.pq import PQ_ANOMALY_TYPES

REQUIRED_PQ_FLEET_TYPES = {
    "frequency_drift",
    "harmonic_injection",
    "phase_imbalance_injection",
    "calibration_drift_correctable",
    "calibration_drift_hardware",
    "replace_inverter",
}


def test_catalogue_covers_every_required_pq_fleet_type():
    assert REQUIRED_PQ_FLEET_TYPES <= catalogue.FLEET_TYPES


def test_every_pq_fleet_type_is_owned_by_fleet():
    for anomaly_type in REQUIRED_PQ_FLEET_TYPES:
        assert catalogue.owner_of(anomaly_type) == "fleet"


def test_pq_anomaly_types_in_pq_module_are_all_registered_in_the_catalogue():
    """ogsim.fleet.pq.PQ_ANOMALY_TYPES (the set PqAnomalyManager actually knows how to apply) must
    be a subset of the catalogue -- no PQ anomaly type exists in the manager without also being
    discoverable/injectable through the control plane (REST/UI/CLI/scenario files)."""
    for anomaly_type in PQ_ANOMALY_TYPES:
        assert anomaly_type in catalogue.BY_ID
        assert catalogue.BY_ID[anomaly_type].owner == "fleet"


def test_frequency_drift_has_a_target_offset_param():
    entry = catalogue.BY_ID["frequency_drift"]
    assert "target_offset_hz" in entry.params
    assert entry.wire_type == "FLEET_FREQUENCY_DRIFT"


def test_harmonic_injection_has_thd_target_and_order_params():
    entry = catalogue.BY_ID["harmonic_injection"]
    assert "thd_target_pct" in entry.params
    assert "order" in entry.params
    assert entry.params["order"]["enum"] == [3, 5, 7]
    assert entry.wire_type == "FLEET_HARMONIC_INJECTION"


def test_phase_imbalance_injection_has_a_bias_kw_param():
    entry = catalogue.BY_ID["phase_imbalance_injection"]
    assert "bias_kw" in entry.params
    assert entry.wire_type == "FLEET_PHASE_IMBALANCE_INJECTION"


def test_calibration_drift_correctable_and_hardware_share_the_same_param_shape():
    correctable = catalogue.BY_ID["calibration_drift_correctable"]
    hardware = catalogue.BY_ID["calibration_drift_hardware"]
    assert (
        set(correctable.params)
        == set(hardware.params)
        == {
            "target_freq_offset_hz",
            "target_voltage_offset_pct",
        }
    )
    assert correctable.wire_type == "FLEET_CALIBRATION_DRIFT_CORRECTABLE"
    assert hardware.wire_type == "FLEET_CALIBRATION_DRIFT_HARDWARE"


def test_replace_inverter_has_new_serial_and_firmware_params():
    entry = catalogue.BY_ID["replace_inverter"]
    assert "new_serial" in entry.params
    assert "new_firmware" in entry.params
    assert entry.wire_type == "FLEET_REPLACE_INVERTER"
    assert entry.target_kind == "hub"


def test_replace_inverter_is_documented_as_an_instantaneous_action_not_a_timed_anomaly():
    entry = catalogue.BY_ID["replace_inverter"]
    assert "instantaneous" in entry.description.lower()


# ---------------------------------------------------------------------------
# site_sag_swell (customer-site voltage sag and swell) -- owner: scada.
# Behavior lives in ogsim.scada (out of this agent's lane); only the
# catalogue metadata this agent's own additions coexist with is verified here.
# ---------------------------------------------------------------------------


def test_site_sag_swell_is_registered_with_sag_and_swell_modes():
    entry = catalogue.BY_ID["site_sag_swell"]
    assert entry.owner == "scada"
    assert entry.params["mode"]["enum"] == ["sag", "swell"]
    assert "pu_level" in entry.params
    assert entry.wire_type == "SCADA_SITE_SAG_SWELL"


def test_site_sag_swell_is_not_claimed_as_a_fleet_pq_anomaly():
    assert "site_sag_swell" not in PQ_ANOMALY_TYPES
    assert "site_sag_swell" not in catalogue.FLEET_TYPES


# ---------------------------------------------------------------------------
# General catalogue hygiene for the new entries.
# ---------------------------------------------------------------------------


def test_every_new_pq_entry_has_a_description():
    for anomaly_type in REQUIRED_PQ_FLEET_TYPES | {"site_sag_swell"}:
        assert catalogue.BY_ID[anomaly_type].description


def test_new_pq_entries_appear_in_as_list():
    listed_ids = {entry["id"] for entry in catalogue.as_list()}
    for anomaly_type in REQUIRED_PQ_FLEET_TYPES | {"site_sag_swell"}:
        assert anomaly_type in listed_ids
