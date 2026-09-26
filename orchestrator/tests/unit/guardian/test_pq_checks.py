"""K14 guardian checks G-21..G-25 (00-invariants.md; 07-delivery/06-service-profiles-and-power-quality.md
S5.3, S5.5.3, S6.7). One negative + one positive test per check, plus the S6.7/TS-16b/TS-18-shaped
negative cases for G-25, mirroring the style of `test_checks.py`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from opengrid.core.models.pq import CalibrationReference
from opengrid.core.pq import CalibrationBounds, OffsetVector, PqEnvelopeLimits, PqMeasurement
from opengrid.guardian import pq_checks
from opengrid.guardian.checks import CheckOutcome
from opengrid.guardian.pq_ports import ProposedCalibrationCommand

LIMITS = PqEnvelopeLimits(
    max_phase_imbalance_pct=2.0,
    voltage_band_pct=3.0,
    freq_tolerance_hz=0.5,
    pf_min=0.95,
    thd_voltage_limit_pct=3.0,
    thd_current_limit_pct=3.0,
)


def _measurement(**overrides: float) -> PqMeasurement:
    base: dict[str, float] = {
        "imbalance_pct": 0.5,
        "voltage_deviation_pct": 0.5,
        "freq_deviation_hz": 0.05,
        "pf": 0.98,
        "thd_voltage_pct": 1.0,
        "thd_current_pct": 1.0,
    }
    base.update(overrides)
    return PqMeasurement(**base)  # type: ignore[arg-type]


# --- G-21 per-phase imbalance ------------------------------------------------------------------------


def test_g21_phase_imbalance_positive():
    assert pq_checks.check_g21_phase_imbalance("bank-1", _measurement(), LIMITS).ok


def test_g21_phase_imbalance_negative():
    r = pq_checks.check_g21_phase_imbalance("bank-1", _measurement(imbalance_pct=5.0), LIMITS)
    assert not r.ok and r.rule_id == "G-21" and r.reason == "PQ_PHASE_IMBALANCE_EXCEEDED"


def test_g21_phase_imbalance_negative_stale_fallback_reason():
    r = pq_checks.check_g21_phase_imbalance("bank-1", _measurement(imbalance_pct=5.0), LIMITS, is_stale=True)
    assert not r.ok and r.reason == "PQ_PHASE_IMBALANCE_STALE_FALLBACK_EXCEEDED"


# --- G-22 THD -----------------------------------------------------------------------------------------


def test_g22_thd_positive():
    assert pq_checks.check_g22_thd("bank-1", _measurement(), LIMITS).ok


def test_g22_thd_negative_current():
    r = pq_checks.check_g22_thd("bank-1", _measurement(thd_current_pct=8.0), LIMITS)
    assert not r.ok and r.rule_id == "G-22" and r.reason == "PQ_THD_EXCEEDED"


def test_g22_thd_negative_voltage():
    r = pq_checks.check_g22_thd("bank-1", _measurement(thd_voltage_pct=8.0), LIMITS)
    assert not r.ok and r.reason == "PQ_THD_EXCEEDED"


# --- G-23 frequency/voltage deviation ------------------------------------------------------------------


def test_g23_freq_voltage_positive():
    assert pq_checks.check_g23_freq_voltage_deviation("bank-1", _measurement(), LIMITS).ok


def test_g23_freq_voltage_negative_frequency():
    r = pq_checks.check_g23_freq_voltage_deviation("bank-1", _measurement(freq_deviation_hz=1.0), LIMITS)
    assert not r.ok and r.rule_id == "G-23" and r.reason == "PQ_FREQ_VOLTAGE_DEVIATION_EXCEEDED"


def test_g23_freq_voltage_negative_voltage():
    r = pq_checks.check_g23_freq_voltage_deviation("bank-1", _measurement(voltage_deviation_pct=10.0), LIMITS)
    assert not r.ok and r.reason == "PQ_FREQ_VOLTAGE_DEVIATION_EXCEEDED"


# --- G-24 ride-through / asset-state conformance --------------------------------------------------------


def test_g24_positive_ok_state_matching_ride_through():
    r = pq_checks.check_g24_asset_conformance(
        "hub-1",
        asset_state="OK",
        hub_ride_through_class="CATEGORY_III",
        envelope_ride_through_class="CATEGORY_III",
        pq_sensitive=True,
    )
    assert r.ok


def test_g24_negative_ride_through_insufficient():
    r = pq_checks.check_g24_asset_conformance(
        "hub-1",
        asset_state="OK",
        hub_ride_through_class="CATEGORY_I",
        envelope_ride_through_class="CATEGORY_III",
        pq_sensitive=True,
    )
    assert not r.ok and r.rule_id == "G-24" and r.reason == "PQ_RIDE_THROUGH_CLASS_INSUFFICIENT"


def test_g24_negative_quarantined_excluded_any_profile():
    r = pq_checks.check_g24_asset_conformance(
        "hub-1",
        asset_state="QUARANTINED",
        hub_ride_through_class="CATEGORY_III",
        envelope_ride_through_class="CATEGORY_III",
        pq_sensitive=False,
    )
    assert not r.ok and r.reason == "PQ_ASSET_STATE_QUARANTINED_EXCLUDED"


def test_g24_negative_degraded_excluded_for_pq_sensitive():
    r = pq_checks.check_g24_asset_conformance(
        "hub-1",
        asset_state="DEGRADED",
        hub_ride_through_class="CATEGORY_III",
        envelope_ride_through_class="CATEGORY_III",
        pq_sensitive=True,
    )
    assert not r.ok and r.reason == "PQ_ASSET_STATE_DEGRADED_EXCLUDED_PQ_SENSITIVE"


def test_g24_positive_degraded_eligible_for_grid_code_minimum():
    """S5.5.3 table: DEGRADED is eligible for ERCOT_ENERGY/arbitrage (grid-code minimum) as long as it
    still meets the (separately-checked, by G-21..G-23) grid-code-minimum envelope."""
    r = pq_checks.check_g24_asset_conformance(
        "hub-1",
        asset_state="DEGRADED",
        hub_ride_through_class="CATEGORY_III",
        envelope_ride_through_class="CATEGORY_III",
        pq_sensitive=False,
    )
    assert r.ok


def test_g24_positive_home_never_gated():
    r = pq_checks.check_g24_asset_conformance(
        "hub-1",
        asset_state="QUARANTINED",
        hub_ride_through_class="CATEGORY_I",
        envelope_ride_through_class="CATEGORY_III",
        pq_sensitive=False,
        is_home=True,
    )
    assert r.ok


def test_g24_negative_unknown_ride_through_class_fails_closed():
    r = pq_checks.check_g24_asset_conformance(
        "hub-1",
        asset_state="OK",
        hub_ride_through_class="CATEGORY_III",
        envelope_ride_through_class="BOGUS",
        pq_sensitive=True,
    )
    assert not r.ok and r.reason == "PQ_RIDE_THROUGH_CLASS_UNKNOWN"


# --- G-25 calibration command safety ---------------------------------------------------------------------

BOUNDS = CalibrationBounds(max_freq_hz=0.05, max_voltage_pct=1.0, max_phase_deg=2.0)


ISSUED_AT = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)


def _command(**overrides: object) -> ProposedCalibrationCommand:
    correction = overrides.get("correction", OffsetVector(freq_hz=0.02, voltage_pct=0.3, phase_deg=1.0))
    bounds = overrides.get("bounds", BOUNDS)
    issued_at = overrides.get("issued_at", ISSUED_AT)
    expires_at = overrides.get("expires_at", ISSUED_AT + timedelta(seconds=60))
    return ProposedCalibrationCommand(
        hub_id="hub-1",
        correction=correction,  # type: ignore[arg-type]
        bounds=bounds,  # type: ignore[arg-type]
        calibration_id=UUID("00000000-0000-4000-8000-0000000000c1"),
        reference=CalibrationReference(
            phase_deg=0.0, freq_hz=60.0, amplitude_v=240.0, sync_source="ntp_disciplined"
        ),
        issued_at=issued_at,  # type: ignore[arg-type]
        expires_at=expires_at,  # type: ignore[arg-type]
    )


def _lease(command: ProposedCalibrationCommand, *, now: datetime = ISSUED_AT) -> CheckOutcome:
    return pq_checks.check_g25_calibration_lease(command, now=now, max_lease_s=300.0, max_issue_skew_s=5.0)


def test_g25_lease_positive():
    assert _lease(_command()).ok


def test_g25_lease_negative_expired():
    r = _lease(_command(), now=ISSUED_AT + timedelta(seconds=60))
    assert not r.ok and r.rule_id == "G-25" and r.reason == "PQ_CALIBRATION_LEASE_INVALID"


def test_g25_lease_negative_future_dated_beyond_skew():
    r = _lease(
        _command(issued_at=ISSUED_AT + timedelta(seconds=30), expires_at=ISSUED_AT + timedelta(seconds=90))
    )
    assert not r.ok and r.reason == "PQ_CALIBRATION_LEASE_INVALID"


def test_g25_lease_negative_too_long_lived():
    r = _lease(_command(expires_at=ISSUED_AT + timedelta(hours=1)))
    assert not r.ok and r.reason == "PQ_CALIBRATION_LEASE_INVALID"


def test_ride_through_rank_is_the_canonical_core_constant():
    """The allocator must not import the guardian for canonical PQ data: one copy, in core.pq."""
    from opengrid.core.pq import RIDE_THROUGH_RANK

    assert pq_checks.RIDE_THROUGH_RANK is RIDE_THROUGH_RANK
    assert (
        RIDE_THROUGH_RANK["CATEGORY_I"] < RIDE_THROUGH_RANK["CATEGORY_II"] < RIDE_THROUGH_RANK["CATEGORY_III"]
    )


def test_g25_positive_within_bounds_no_grant_rate_ok():
    r = pq_checks.check_g25_calibration_safety(
        _command(),
        firmware_max_bounds=BOUNDS,
        last_attempt_epoch_s=None,
        now_epoch_s=1_000_000.0,
        min_interval_s=86_400.0,
        hub_has_active_sensitive_grant=False,
    )
    assert r.ok


def test_g25_negative_active_sensitive_grant_ts18():
    """TS-18/ES18: G-25 refuses while the target hub carries an active committed grant for a
    non-default-envelope obligation, even though bounds/rate limit are otherwise fine."""
    r = pq_checks.check_g25_calibration_safety(
        _command(),
        firmware_max_bounds=BOUNDS,
        last_attempt_epoch_s=None,
        now_epoch_s=1_000_000.0,
        min_interval_s=86_400.0,
        hub_has_active_sensitive_grant=True,
    )
    assert not r.ok and r.rule_id == "G-25" and r.reason == "PQ_CALIBRATION_ACTIVE_SENSITIVE_GRANT"


def test_g25_negative_rate_limit_exceeded_ts16b():
    """TS-16b: a calibration command issued twice within the 24h rate limit is refused."""
    r = pq_checks.check_g25_calibration_safety(
        _command(),
        firmware_max_bounds=BOUNDS,
        last_attempt_epoch_s=1_000_000.0,
        now_epoch_s=1_000_000.0 + 3600.0,  # 1h later, well inside the 24h window
        min_interval_s=86_400.0,
        hub_has_active_sensitive_grant=False,
    )
    assert not r.ok and r.reason == "PQ_CALIBRATION_RATE_LIMIT_EXCEEDED"


def test_g25_negative_bounds_exceed_firmware_limit_ts16b():
    """TS-16b: a calibration command exceeding configured (firmware) bounds is refused."""
    r = pq_checks.check_g25_calibration_safety(
        _command(bounds=CalibrationBounds(max_freq_hz=1.0, max_voltage_pct=1.0, max_phase_deg=2.0)),
        firmware_max_bounds=BOUNDS,
        last_attempt_epoch_s=None,
        now_epoch_s=1_000_000.0,
        min_interval_s=86_400.0,
        hub_has_active_sensitive_grant=False,
    )
    assert not r.ok and r.reason == "PQ_CALIBRATION_BOUNDS_EXCEED_FIRMWARE_LIMIT"


def test_g25_negative_correction_exceeds_its_own_bounds():
    r = pq_checks.check_g25_calibration_safety(
        _command(correction=OffsetVector(freq_hz=0.2, voltage_pct=0.3, phase_deg=1.0)),
        firmware_max_bounds=BOUNDS,
        last_attempt_epoch_s=None,
        now_epoch_s=1_000_000.0,
        min_interval_s=86_400.0,
        hub_has_active_sensitive_grant=False,
    )
    assert not r.ok and r.reason == "PQ_CALIBRATION_CORRECTION_EXCEEDS_BOUNDS"


def test_g25_positive_rate_limit_cleared_after_interval():
    r = pq_checks.check_g25_calibration_safety(
        _command(),
        firmware_max_bounds=BOUNDS,
        last_attempt_epoch_s=1_000_000.0,
        now_epoch_s=1_000_000.0 + 86_400.0,
        min_interval_s=86_400.0,
        hub_has_active_sensitive_grant=False,
    )
    assert r.ok
