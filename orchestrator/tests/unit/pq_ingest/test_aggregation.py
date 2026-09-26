"""opengrid.pq_ingest.aggregation: measured per-bank aggregation from
PqWaveformSummaryRow (S6.5 step 3), incl. the K1 stale-data exclusion pattern."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
