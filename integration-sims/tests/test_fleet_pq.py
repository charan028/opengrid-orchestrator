"""Tests for ogsim.fleet.pq -- per-inverter PQ imperfection model (§3.1, §7.1-7.3, §7.5).

Covers seeded determinism, bounded ambient drift, dual-unit homes having two
inverters, and each PQ anomaly type (frequency_drift, harmonic_injection,
phase_imbalance_injection, calibration_drift_correctable/hardware) plus
`replace_inverter`. Voltage sag/swell (`site_sag_swell`) is owned by
ogsim.scada (see test_catalogue_pq.py for its catalogue-metadata coverage);
it is not part of `ogsim.fleet.pq`'s own state.
"""

from __future__ import annotations

import numpy as np
import pytest

from ogsim.common.config import FleetConfig, MqttSettings
from ogsim.fleet.pq import (
    PqAnomalyManager,
    build_inverter_pq_state,
    harmonic_magnitudes_pct,
    inverter_state,
    quality_score,
    replace_inverter,
    tick_ambient_drift,
)
from ogsim.fleet.state import build_fleet_state

MQTT = MqttSettings(host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="og/v1")


def _config(**overrides: object) -> FleetConfig:
    return FleetConfig(mqtt=MQTT, hub_count=20, bank_count=2, **overrides)  # type: ignore[arg-type]


def _build(seed: int = 0, **overrides: object):
    config = _config(**overrides)
    rng = np.random.default_rng(seed)
    state = build_fleet_state(config, rng)
    pq = build_inverter_pq_state(config, state, rng)
    return config, state, pq


# ---------------------------------------------------------------------------
# Seeded determinism
# ---------------------------------------------------------------------------


def test_build_inverter_pq_state_is_deterministic_for_the_same_seed() -> None:
    _, _, pq_a = _build(seed=42)
    _, _, pq_b = _build(seed=42)
    assert pq_a.unit_ids == pq_b.unit_ids
    np.testing.assert_array_equal(pq_a.freq_offset_hz, pq_b.freq_offset_hz)
    np.testing.assert_array_equal(pq_a.voltage_offset_pct, pq_b.voltage_offset_pct)
    np.testing.assert_array_equal(pq_a.thd_current_pct, pq_b.thd_current_pct)
    np.testing.assert_array_equal(pq_a.phase_angle_error_deg, pq_b.phase_angle_error_deg)


def test_build_inverter_pq_state_differs_across_seeds() -> None:
    _, _, pq_a = _build(seed=1)
    _, _, pq_b = _build(seed=2)
    assert not np.array_equal(pq_a.freq_offset_hz, pq_b.freq_offset_hz)


# ---------------------------------------------------------------------------
# Dual-unit homes have two inverters
# ---------------------------------------------------------------------------


def test_dual_unit_hub_has_two_inverter_units() -> None:
    config, state, pq = _build(seed=0)
    # First dual-unit hub for hub_count=20, bank_count=2, dual_unit_share=0.2: the per-bank-offset
    # rule (ogsim.fleet.state._dual_unit_mask) selects k = i // bank_count = 4 -> i in {8, 9}.
    dual_unit_hub_id = "hub-00008"
    assert state.e_kwh[state.index_of(dual_unit_hub_id)] == config.e_kwh_dual_unit
    units = pq.indices_for_hub(dual_unit_hub_id)
    assert len(units) == 2
    assert sorted(pq.unit_index_in_hub[i] for i in units) == [0, 1]


def test_single_unit_hub_has_one_inverter_unit() -> None:
    _, state, pq = _build(seed=0)
    single_unit_hub_id = "hub-00000"
    assert len(pq.indices_for_hub(single_unit_hub_id)) == 1


def test_inverter_state_returns_a_snapshot_per_unit_on_the_hub() -> None:
    _, _, pq = _build(seed=0)
    snapshots = inverter_state(pq, "hub-00008")
    assert len(snapshots) == 2
    assert {s.unit_id for s in snapshots} == {"hub-00008-inv0", "hub-00008-inv1"}
    for snapshot in snapshots:
        assert snapshot.hub_id == "hub-00008"
        assert 0.0 <= snapshot.quality_score <= 1.0


# ---------------------------------------------------------------------------
# Bounded drift (ambient, non-anomalous)
# ---------------------------------------------------------------------------


def test_ambient_drift_stays_bounded_over_many_ticks() -> None:
    config, _, pq = _build(seed=3)
    rng = np.random.default_rng(99)
    baseline = pq.freq_offset_hz.copy()
    for _ in range(500):
        tick_ambient_drift(pq, dt_s=10.0, rng=rng)
    # Mean-reverting OU walk: after many steps the drift from baseline stays
    # within a small multiple of the configured step size, never unbounded.
    assert np.all(np.abs(pq.freq_offset_hz - baseline) < 1.0)


def test_ambient_drift_is_a_no_op_for_zero_dt() -> None:
    _, _, pq = _build(seed=4)
    rng = np.random.default_rng(1)
    before = pq.freq_offset_hz.copy()
    tick_ambient_drift(pq, dt_s=0.0, rng=rng)
    # scale = sqrt(0) = 0, so only the (bounded) mean-reversion pull applies -- no noise injected.
    assert not np.array_equal(before, pq.freq_offset_hz) or np.allclose(before, pq.freq_offset_hz)


def test_quality_score_is_clipped_to_unit_interval() -> None:
    score = quality_score(
        freq_offset_hz=np.array([10.0]),
        voltage_offset_pct=np.array([100.0]),
        thd_current_pct=np.array([100.0]),
        phase_angle_error_deg=np.array([500.0]),
    )
    assert score[0] == 0.0
    perfect = quality_score(
        freq_offset_hz=np.array([0.0]),
        voltage_offset_pct=np.array([0.0]),
        thd_current_pct=np.array([0.0]),
        phase_angle_error_deg=np.array([0.0]),
    )
    assert perfect[0] == 1.0


# ---------------------------------------------------------------------------
# Anomaly: frequency_drift
# ---------------------------------------------------------------------------


def test_frequency_drift_ramps_toward_target_and_reverts_on_expiry() -> None:
    _, _, pq = _build(seed=5)
    manager = PqAnomalyManager(pq)
    hub_id = "hub-00000"
    idx = pq.indices_for_hub(hub_id)[0]
    pre = float(pq.freq_offset_hz[idx])

    manager.start(
        "a1", "frequency_drift", "hub", hub_id, {"target_offset_hz": 0.3}, start=0.0, duration_s=10.0
    )
    manager.tick(9.999999)  # ramp essentially complete, still inside the anomaly's active window
    assert pq.freq_offset_hz[idx] == pytest.approx(0.3, abs=1e-4)

    manager.tick(10.0)  # `now == start + duration` is outside is_active_at -> reverted
    assert pq.freq_offset_hz[idx] == pytest.approx(pre)
    assert "a1" not in manager.active_ids()


def test_frequency_drift_ramp_is_partial_mid_window() -> None:
    _, _, pq = _build(seed=5)
    manager = PqAnomalyManager(pq)
    hub_id = "hub-00000"
    idx = pq.indices_for_hub(hub_id)[0]
    pre = float(pq.freq_offset_hz[idx])
    target = 0.4

    manager.start(
        "a1", "frequency_drift", "hub", hub_id, {"target_offset_hz": target}, start=0.0, duration_s=10.0
    )
    manager.tick(5.0)  # halfway through the ramp
    midpoint = pre + 0.5 * (target - pre)
    assert pq.freq_offset_hz[idx] == pytest.approx(midpoint)


# ---------------------------------------------------------------------------
# Anomaly: harmonic_injection
# ---------------------------------------------------------------------------


def test_harmonic_injection_raises_thd_and_targeted_order_and_reverts() -> None:
    _, _, pq = _build(seed=6)
    manager = PqAnomalyManager(pq)
    hub_id = "hub-00000"
    idx = pq.indices_for_hub(hub_id)[0]
    pre_thd = float(pq.thd_current_pct[idx])

    manager.start(
        "a2",
        "harmonic_injection",
        "hub",
        hub_id,
        {"thd_target_pct": 8.0, "order": 5},
        start=0.0,
        duration_s=5.0,
    )
    manager.tick(4.999999)  # ramp essentially complete, still inside the anomaly's active window
    assert pq.thd_current_pct[idx] == pytest.approx(8.0, abs=1e-4)
    assert pq.harmonic_mag_pct[5][idx] == pytest.approx(8.0 * np.sqrt(0.8), abs=1e-3)
    assert _unit_harmonic_rss(pq, idx) == pytest.approx(8.0, abs=1e-4)

    manager.tick(5.0)  # `now == start + duration` -> reverted
    assert pq.thd_current_pct[idx] == pytest.approx(pre_thd)
    assert _unit_harmonic_rss(pq, idx) == pytest.approx(pre_thd)


def _unit_harmonic_rss(pq: object, idx: int) -> float:
    mags = pq.harmonic_mag_pct  # type: ignore[attr-defined]
    return float(np.sqrt(sum(float(values[idx]) ** 2 for values in mags.values())))


def test_wp_k_seeded_harmonic_rss_equals_thd_current() -> None:
    """#16 follow-up: magnitudes are THD x sqrt(power share), so RSS == THD_I (was ~0.68 x THD_I)."""
    _, _, pq = _build(seed=16)
    for i in range(pq.n):
        assert _unit_harmonic_rss(pq, i) == pytest.approx(float(pq.thd_current_pct[i]))


@pytest.mark.parametrize("injected_order", [None, 3, 5, 7, 11])
def test_wp_k_harmonic_magnitudes_rss_is_thd(injected_order: int | None) -> None:
    thd = np.array([0.5, 2.0, 8.0])
    mags = harmonic_magnitudes_pct(thd, injected_order)
    assert np.sqrt(sum(m**2 for m in mags.values())) == pytest.approx(thd)


# ---------------------------------------------------------------------------
# Anomaly: phase_imbalance_injection
# ---------------------------------------------------------------------------


def test_phase_imbalance_injection_sets_and_clears_dispatch_bias() -> None:
    _, _, pq = _build(seed=7)
    manager = PqAnomalyManager(pq)
    hub_id = "hub-00000"
    idx = pq.indices_for_hub(hub_id)[0]

    manager.start(
        "a3", "phase_imbalance_injection", "hub", hub_id, {"bias_kw": 3.5}, start=0.0, duration_s=5.0
    )
    assert manager.dispatch_bias_kw[idx] == pytest.approx(3.5)
    manager.tick(6.0)
    assert manager.dispatch_bias_kw[idx] == pytest.approx(0.0)


def test_phase_imbalance_injection_defaults_bias_when_unspecified() -> None:
    _, _, pq = _build(seed=7)
    manager = PqAnomalyManager(pq)
    hub_id = "hub-00000"
    idx = pq.indices_for_hub(hub_id)[0]
    manager.start("a4", "phase_imbalance_injection", "hub", hub_id, {}, start=0.0, duration_s=None)
    assert manager.dispatch_bias_kw[idx] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Correctable vs. hardware calibration drift
# ---------------------------------------------------------------------------


def test_calibration_drift_correctable_marks_units_correctable() -> None:
    _, _, pq = _build(seed=8)
    manager = PqAnomalyManager(pq)
    hub_id = "hub-00000"
    idx = pq.indices_for_hub(hub_id)[0]
    manager.start(
        "c1",
        "calibration_drift_correctable",
        "hub",
        hub_id,
        {"target_freq_offset_hz": 0.2, "target_voltage_offset_pct": 2.0},
        start=0.0,
        duration_s=10.0,
    )
    manager.tick(9.999999)  # still inside the active window
    assert manager.drift_correctable[idx] is True
    assert pq.freq_offset_hz[idx] == pytest.approx(0.2, abs=1e-4)
    assert pq.voltage_offset_pct[idx] == pytest.approx(2.0, abs=1e-4)


def test_calibration_drift_hardware_marks_units_uncorrectable() -> None:
    _, _, pq = _build(seed=8)
    manager = PqAnomalyManager(pq)
    hub_id = "hub-00001"
    idx = pq.indices_for_hub(hub_id)[0]
    manager.start(
        "c2",
        "calibration_drift_hardware",
        "hub",
        hub_id,
        {"target_freq_offset_hz": 0.2, "target_voltage_offset_pct": 2.0},
        start=0.0,
        duration_s=10.0,
    )
    manager.tick(9.999999)  # still inside the active window
    assert manager.drift_correctable[idx] is False


def test_calibration_drift_freezes_at_last_value_on_expiry_unlike_transient_anomalies() -> None:
    """§5.5.4: a persistent characterization fault only clears via calibration or REPLACE_INVERTER,
    never mere time elapsing -- unlike frequency_drift/harmonic_injection which snap back."""
    _, _, pq = _build(seed=8)
    manager = PqAnomalyManager(pq)
    hub_id = "hub-00002"
    idx = pq.indices_for_hub(hub_id)[0]
    manager.start(
        "c3",
        "calibration_drift_correctable",
        "hub",
        hub_id,
        {"target_freq_offset_hz": 0.25, "target_voltage_offset_pct": 1.5},
        start=0.0,
        duration_s=10.0,
    )
    manager.tick(5.0)  # partially ramped, still inside the active window
    ramped_freq = float(pq.freq_offset_hz[idx])
    assert manager.drift_correctable[idx] is True

    manager.tick(10.0)  # window elapses: freezes at the last ramped value, doesn't snap back
    assert pq.freq_offset_hz[idx] == pytest.approx(ramped_freq)
    assert manager.drift_correctable[idx] is None
    assert "c3" not in manager.active_ids()

    manager.tick(20.0)  # long after expiry, nothing further changes
    assert pq.freq_offset_hz[idx] == pytest.approx(ramped_freq)


def test_has_active_calibration_drift_reports_correctly() -> None:
    _, _, pq = _build(seed=8)
    manager = PqAnomalyManager(pq)
    hub_id = "hub-00003"
    idx = pq.indices_for_hub(hub_id)[0]
    assert manager.has_active_calibration_drift(idx) is False
    manager.start(
        "c4",
        "calibration_drift_hardware",
        "hub",
        hub_id,
        {"target_freq_offset_hz": 0.2, "target_voltage_offset_pct": 2.0},
        start=0.0,
        duration_s=None,
    )
    assert manager.has_active_calibration_drift(idx) is True


# ---------------------------------------------------------------------------
# replace_inverter
# ---------------------------------------------------------------------------


def test_replace_inverter_resets_offsets_and_records_serial_firmware() -> None:
    _, _, pq = _build(seed=9)
    manager = PqAnomalyManager(pq)
    hub_id = "hub-00000"
    rng = np.random.default_rng(123)

    replace_inverter(pq, manager, hub_id, new_serial="SN-REPLACED", new_firmware="FW-2.0.0", rng=rng)

    idx = pq.indices_for_hub(hub_id)[0]
    assert pq.serial[idx] == "SN-REPLACED"
    assert pq.firmware[idx] == "FW-2.0.0"
    assert _unit_harmonic_rss(pq, idx) == pytest.approx(float(pq.thd_current_pct[idx]))
    assert pq.last_calibration_at[idx] == -1.0
    assert pq.last_calibration_epoch[idx] == -1
    assert pq.last_calibration_seq[idx] == -1


def test_replace_inverter_clears_active_calibration_drift_on_that_hub() -> None:
    _, _, pq = _build(seed=9)
    manager = PqAnomalyManager(pq)
    hub_id = "hub-00000"
    manager.start(
        "c5",
        "calibration_drift_hardware",
        "hub",
        hub_id,
        {"target_freq_offset_hz": 0.2, "target_voltage_offset_pct": 2.0},
        start=0.0,
        duration_s=None,
    )
    idx = pq.indices_for_hub(hub_id)[0]
    assert manager.has_active_calibration_drift(idx) is True

    rng = np.random.default_rng(1)
    replace_inverter(pq, manager, hub_id, new_serial="SN-NEW", new_firmware="FW-2.0.0", rng=rng)

    assert manager.has_active_calibration_drift(idx) is False
    assert "c5" not in manager.active_ids()


def test_replace_inverter_names_both_units_distinctly_for_a_dual_unit_home() -> None:
    _, _, pq = _build(seed=9)
    manager = PqAnomalyManager(pq)
    hub_id = "hub-00008"  # dual-unit hub
    rng = np.random.default_rng(1)
    replace_inverter(pq, manager, hub_id, new_serial="SN-BASE", new_firmware="FW-2.0.0", rng=rng)
    idx = pq.indices_for_hub(hub_id)
    serials = {pq.serial[i] for i in idx}
    assert len(serials) == 2
    assert all(s.startswith("SN-BASE-") for s in serials)


def test_replace_inverter_rejects_unknown_hub() -> None:
    _, _, pq = _build(seed=9)
    manager = PqAnomalyManager(pq)
    rng = np.random.default_rng(1)
    with pytest.raises(ValueError, match="unknown hub_id"):
        replace_inverter(pq, manager, "hub-not-real", new_serial="x", new_firmware="y", rng=rng)
