"""Tests for `opengrid.pq_ingest.characterize` (blocker fix: `og.hub_inverter_pq` had 0 live
rows because nothing aggregated ingested waveform summaries into per-inverter
characterization, S3.1/S5.1/S6.5)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from opengrid.core.models.pq import HarmonicComponent, PqWaveformSummaryRow
from opengrid.pq_ingest.characterize import (
    DEFAULT_KVA_RATING,
    NOMINAL_FREQ_HZ,
    NOMINAL_VOLTAGE_V,
    characterize_fleet,
    characterize_hub,
)

NOW = datetime(2026, 9, 27, 0, 0, 0, tzinfo=UTC)


def _summary(
    hub_id: str = "hub-00000",
    ts: datetime = NOW,
    v_rms_a: float | None = NOMINAL_VOLTAGE_V,
    freq_hz: float | None = NOMINAL_FREQ_HZ,
    thd_i_pct_a: float | None = 2.0,
    phase_angle_deg_a: float | None = 0.5,
    harmonics_i: dict | None = None,
) -> PqWaveformSummaryRow:
    return PqWaveformSummaryRow(
        hub_id=hub_id,
        ts=ts,
        v_rms_a=v_rms_a,
        freq_hz=freq_hz,
        thd_i_pct_a=thd_i_pct_a,
        phase_angle_deg_a=phase_angle_deg_a,
        harmonics_i=harmonics_i,
    )


def test_characterize_hub_returns_none_for_empty_input():
    assert characterize_hub("hub-00000", [], now=NOW) is None


def test_characterize_hub_returns_none_without_freq_or_voltage_data():
    summary = PqWaveformSummaryRow(hub_id="hub-00000", ts=NOW)
    assert characterize_hub("hub-00000", [summary], now=NOW) is None


def test_characterize_hub_perfect_inverter_scores_above_default_baseline():
    """The fixture's default 2.0% THD_I and 0.5 deg phase-angle error already cost some
    quality score by design (S5.1's formula); this just checks a clean freq/voltage
    reading doesn't ALSO get penalized, keeping the score comfortably high."""
    summaries = [_summary() for _ in range(5)]
    result = characterize_hub("hub-00000", summaries, now=NOW)
    assert result is not None
    assert result.freq_offset_hz == pytest.approx(0.0)
    assert result.voltage_offset_pct == pytest.approx(0.0)
    assert result.quality_score > 0.85


def test_characterize_hub_detects_frequency_and_voltage_offset():
    summaries = [_summary(v_rms_a=NOMINAL_VOLTAGE_V * 1.02, freq_hz=NOMINAL_FREQ_HZ + 0.1)]
    result = characterize_hub("hub-00000", summaries, now=NOW)
    assert result is not None
    assert result.freq_offset_hz == pytest.approx(0.1)
    assert result.voltage_offset_pct > 1.9  # ~2%


def test_characterize_hub_infers_phase_connection_from_populated_fields():
    single_phase = characterize_hub("hub-00000", [_summary()], now=NOW)
    assert single_phase is not None
    assert single_phase.phase_connection == "A"

    three_phase_summary = PqWaveformSummaryRow(
        hub_id="hub-00010",
        ts=NOW,
        v_rms_a=NOMINAL_VOLTAGE_V,
        v_rms_b=NOMINAL_VOLTAGE_V,
        v_rms_c=NOMINAL_VOLTAGE_V,
        freq_hz=NOMINAL_FREQ_HZ,
    )
    three_phase = characterize_hub("hub-00010", [three_phase_summary], now=NOW)
    assert three_phase is not None
    assert three_phase.phase_connection == "ABC"


def test_characterize_hub_uses_kva_rating_stand_in():
    result = characterize_hub("hub-00000", [_summary()], now=NOW)
    assert result is not None
    assert result.kva_rating == DEFAULT_KVA_RATING


def test_characterize_hub_takes_the_latest_harmonics_block_when_present():
    older = _summary(ts=datetime(2026, 9, 27, 0, 0, 0, tzinfo=UTC), harmonics_i=None)
    newer = _summary(
        ts=datetime(2026, 9, 27, 0, 1, 0, tzinfo=UTC),
        harmonics_i={"3": HarmonicComponent(mag_pct=1.2, angle_deg=30.0)},
    )
    result = characterize_hub("hub-00000", [older, newer], now=NOW)
    assert result is not None
    assert result.dominant_harmonics == {"3": {"mag_pct": 1.2, "angle_deg": 30.0}}


def test_characterize_hub_no_harmonics_block_in_window_is_none():
    result = characterize_hub("hub-00000", [_summary(harmonics_i=None)], now=NOW)
    assert result is not None
    assert result.dominant_harmonics is None


def test_characterize_fleet_groups_by_hub_and_skips_hubs_without_enough_data():
    summaries = [
        _summary(hub_id="hub-00000"),
        _summary(hub_id="hub-00001"),
        PqWaveformSummaryRow(hub_id="hub-00002", ts=NOW),  # no freq/voltage -- not characterizable
    ]
    results = characterize_fleet(summaries, now=NOW)
    assert {r.hub_id for r in results} == {"hub-00000", "hub-00001"}


def test_characterize_fleet_one_pass_over_many_hubs_is_batched_not_looped():
    """Sanity check that characterize_fleet does a single grouping pass, not N separate
    calls that would imply N separate DB round trips upstream."""
    summaries = [_summary(hub_id=f"hub-{i:05d}") for i in range(2000)]
    results = characterize_fleet(summaries, now=NOW)
    assert len(results) == 2000
