"""opengrid.pq_ingest.aggregation: measured per-bank aggregation from
PqWaveformSummaryRow (S6.5 step 3), incl. the K1 stale-data exclusion pattern."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from opengrid.core.models.pq import PqWaveformSummaryRow
from opengrid.pq_ingest.aggregation import bank_measurement, fresh_summaries

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


def _row(hub_id: str, *, age_s: float = 0.0, **kwargs: object) -> PqWaveformSummaryRow:
    defaults: dict[str, object] = {
        "hub_id": hub_id,
        "ts": NOW - timedelta(seconds=age_s),
        "v_rms_a": 240.0,
        "i_rms_a": 10.0,
        "freq_hz": 60.0,
        "pf_a": 0.99,
        "thd_v_pct_a": 1.0,
        "thd_i_pct_a": 2.0,
    }
    defaults.update(kwargs)
    return PqWaveformSummaryRow(**defaults)


def test_fresh_summaries_excludes_stale() -> None:
    fresh = _row("hub-00000", age_s=1.0)
    stale = _row("hub-00001", age_s=100.0)
    result = fresh_summaries([fresh, stale], NOW, freshness_s=10.0)
    assert result == [fresh]


def test_bank_measurement_none_when_no_fresh_summaries() -> None:
    stale = _row("hub-00000", age_s=100.0)
    assert bank_measurement([stale], NOW, freshness_s=10.0) is None


def test_bank_measurement_none_when_missing_voltage_and_frequency() -> None:
    row = PqWaveformSummaryRow(hub_id="hub-00000", ts=NOW)
    assert bank_measurement([row], NOW, freshness_s=10.0) is None


def test_bank_measurement_aggregates_across_hubs_on_one_phase() -> None:
    rows = [_row("hub-00000"), _row("hub-00001", i_rms_a=12.0)]
    measurement = bank_measurement(rows, NOW, freshness_s=10.0)
    assert measurement is not None
    assert measurement.current_a == 22.0  # 10 + 12
    assert measurement.voltage_deviation_pct == 0.0  # both at nominal 240 V
    assert measurement.freq_deviation_hz == 0.0
    assert measurement.thd_current_pct == 2.0


def test_bank_measurement_imbalance_across_three_phases() -> None:
    rows = [
        _row("hub-00000", i_rms_a=100.0, i_rms_b=None, i_rms_c=None),
        _row("hub-00001", i_rms_a=None, v_rms_a=None, v_rms_b=240.0, i_rms_b=100.0, i_rms_c=None),
        _row("hub-00002", i_rms_a=None, v_rms_a=None, v_rms_c=240.0, i_rms_c=50.0, i_rms_b=None),
    ]
    measurement = bank_measurement(rows, NOW, freshness_s=10.0)
    assert measurement is not None
    # A=100, B=100, C=50 -> mean=83.33, max deviation is C at 33.33 -> ~40% imbalance.
    assert measurement.imbalance_pct > 0.0


def test_bank_measurement_excludes_stale_hub_from_aggregate() -> None:
    fresh = _row("hub-00000", i_rms_a=10.0)
    stale = _row("hub-00001", age_s=999.0, i_rms_a=1000.0)
    measurement = bank_measurement([fresh, stale], NOW, freshness_s=10.0)
    assert measurement is not None
    assert measurement.current_a == 10.0


# ---------------------------------------------------------------------------
# S3.2(b) vector-sum THD_I fix (TS-15b/PR #16 cross-system defect: bank_measurement
# reported the MEAN of hubs' THD_I%, not the harmonic vector sum -- overstating bank PQ
# and risking false G-22 vetoes/ladder actions).
# ---------------------------------------------------------------------------


def test_bank_thd_current_pct_cancels_when_harmonics_are_out_of_phase() -> None:
    """Two hubs with equal-magnitude 3rd-harmonic current at OPPOSITE phase angles must
    (nearly) cancel in the bank aggregate -- the defining S3.2(b) behaviour a plain mean of
    THD% can never reproduce (a mean of two 4% readings is always 4%, never ~0%)."""
    rows = [
        _row(
            "hub-00000",
            i_rms_a=10.0,
            thd_i_pct_a=4.0,
            harmonics_i={"3": {"mag_pct": 4.0, "angle_deg": 0.0}},
        ),
        _row(
            "hub-00001",
            i_rms_a=10.0,
            thd_i_pct_a=4.0,
            harmonics_i={"3": {"mag_pct": 4.0, "angle_deg": 180.0}},
        ),
    ]
    measurement = bank_measurement(rows, NOW, freshness_s=10.0)
    assert measurement is not None
    # The naive mean (the old, buggy behaviour) would report 4.0%; the true vector-summed
    # value is ~0% (exact cancellation at 180 degrees apart, equal magnitude).
    assert measurement.thd_current_pct < 0.5
    assert measurement.thd_current_pct != 4.0


def test_bank_thd_current_pct_stacks_when_harmonics_are_in_phase() -> None:
    """The other S3.2(b) extreme: identical phase angle stacks LINEARLY (worst case), so
    the bank THD_I% here equals the common per-hub value, same as a mean would report --
    this is the case the OLD mean-based code got right by coincidence, so the fix must not
    regress it."""
    rows = [
        _row(
            "hub-00000",
            i_rms_a=10.0,
            harmonics_i={"3": {"mag_pct": 4.0, "angle_deg": 0.0}},
        ),
        _row(
            "hub-00001",
            i_rms_a=10.0,
            harmonics_i={"3": {"mag_pct": 4.0, "angle_deg": 0.0}},
        ),
    ]
    measurement = bank_measurement(rows, NOW, freshness_s=10.0)
    assert measurement is not None
    assert measurement.thd_current_pct == pytest.approx(4.0, abs=1e-6)


def test_bank_thd_current_pct_diverse_phase_reproduces_reported_defect_magnitude() -> None:
    """Reproduces the shape of the cross-system defect report (seed 15: ~2.12% reported by
    the mean vs. ~0.18% true): several hubs with the SAME THD_I% magnitude but harmonic
    phase angles spread evenly around the circle largely cancel, while a naive mean of
    their individual THD% stays flat at that magnitude regardless of phase."""
    n = 8
    thd_pct = 2.1
    rows = [
        _row(
            f"hub-{i:05d}",
            i_rms_a=10.0,
            thd_i_pct_a=thd_pct,
            harmonics_i={"3": {"mag_pct": thd_pct, "angle_deg": 360.0 * i / n}},
        )
        for i in range(n)
    ]
    measurement = bank_measurement(rows, NOW, freshness_s=10.0)
    assert measurement is not None
    naive_mean = thd_pct
    assert measurement.thd_current_pct < naive_mean / 5.0  # materially below the old mean-based value


def test_bank_thd_current_pct_falls_back_conservatively_without_harmonics_phase() -> None:
    """A hub with a THD_I% reading but no `harmonics_i` block this cycle (S6.4b's
    harmonic-detail sub-block publishes less often than the fast sub-block) must still
    contribute -- via the S4.a-documented conservative (worst-case/stacking) rule -- rather
    than being silently dropped from the aggregate or vector-summed with a phase it doesn't
    have."""
    rows = [_row("hub-00000", i_rms_a=10.0, thd_i_pct_a=3.0, harmonics_i=None)]
    measurement = bank_measurement(rows, NOW, freshness_s=10.0)
    assert measurement is not None
    assert measurement.thd_current_pct == pytest.approx(3.0, abs=1e-6)


def test_bank_thd_current_pct_mixes_vector_known_and_fallback_hubs() -> None:
    """A bank with one hub reporting harmonic phase (which would cancel with an identical
    opposite-phase hub, if there were one) and one hub with no phase data at all: the
    fallback hub's contribution must still show up in the total (never silently dropped),
    while the known-phase hub's own contribution is computed via the real vector-sum path."""
    rows = [
        _row(
            "hub-00000",
            i_rms_a=10.0,
            harmonics_i={"3": {"mag_pct": 4.0, "angle_deg": 0.0}},
        ),
        _row("hub-00001", i_rms_a=10.0, thd_i_pct_a=4.0, harmonics_i=None),
    ]
    measurement = bank_measurement(rows, NOW, freshness_s=10.0)
    assert measurement is not None
    # Both hubs contribute the same magnitude with the same current -- combined (one via
    # vector sum with itself, one via scalar fallback) this must equal the common value,
    # not zero (a fallback hub must never be able to "disappear" the total).
    assert measurement.thd_current_pct == pytest.approx(4.0, abs=1e-6)


def test_bank_thd_current_pct_hub_without_current_reading_excluded() -> None:
    """K1: a hub with a harmonics_i block but no i_rms reading has no fundamental-current
    base to scale the harmonics against, so it must be excluded, not treated as 0 A."""
    rows = [
        _row("hub-00000", i_rms_a=10.0, thd_i_pct_a=2.0),
        _row(
            "hub-00001",
            i_rms_a=None,
            v_rms_a=None,
            v_rms_b=240.0,
            harmonics_i={"3": {"mag_pct": 99.0, "angle_deg": 0.0}},
        ),
    ]
    measurement = bank_measurement(rows, NOW, freshness_s=10.0)
    assert measurement is not None
    assert measurement.thd_current_pct == pytest.approx(2.0, abs=1e-6)
