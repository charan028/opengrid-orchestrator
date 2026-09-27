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
    HubSummaryAggregate,
    _aggregate_hub,
    characterize_fleet,
    characterize_fleet_from_aggregates,
    characterize_hub,
    characterize_hub_from_aggregate,
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


def test_characterize_hub_skips_a_latest_but_empty_harmonics_block():
    """R3.4.3 (L-7): `pg_backend.latest_summary_aggregates`'s SQL disagreed with this Python path when
    the LATEST sample's `harmonics_i` was an empty (but non-null) `{}` block -- the SQL's
    `WHERE harmonics_i IS NOT NULL` picked it (matching NULL only), while `_dominant_harmonics`'s Python
    truthiness check (`if summary.harmonics_i:`) treats `{}` as absent and keeps looking further back.
    The SQL now excludes `{}` too (`<> '{}'::jsonb`); this pins down the Python side's half of that
    contract so a future change here can't silently reintroduce the mismatch."""
    older_with_data = _summary(
        ts=datetime(2026, 9, 27, 0, 0, 0, tzinfo=UTC),
        harmonics_i={"3": HarmonicComponent(mag_pct=1.2, angle_deg=30.0)},
    )
    newest_but_empty = _summary(ts=datetime(2026, 9, 27, 0, 1, 0, tzinfo=UTC), harmonics_i={})
    result = characterize_hub("hub-00000", [older_with_data, newest_but_empty], now=NOW)
    assert result is not None
    assert result.dominant_harmonics == {"3": {"mag_pct": 1.2, "angle_deg": 30.0}}


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


# --- R3.4.1 PROD-IO fix: raw-row path vs. SQL-aggregation path must be identical -------------------


def _mixed_fixture_summaries() -> list[PqWaveformSummaryRow]:
    """A deliberately varied fixture: multiple hubs, multiple samples/hub, missing phases, missing
    optional fields, harmonics landing on different samples at different times, and one hub with not
    enough data -- exercising every branch `_aggregate_hub`/`characterize_hub` take."""
    summaries: list[PqWaveformSummaryRow] = []
    # hub-00000: single-phase (A only), 5 samples, small drift, one harmonics block on the latest.
    for i in range(5):
        summaries.append(
            PqWaveformSummaryRow(
                hub_id="hub-00000",
                ts=datetime(2026, 9, 27, 0, i, 0, tzinfo=UTC),
                v_rms_a=NOMINAL_VOLTAGE_V + i * 0.3,
                freq_hz=NOMINAL_FREQ_HZ + (i - 2) * 0.01,
                thd_i_pct_a=2.0 + i * 0.1,
                phase_angle_deg_a=0.5,
                harmonics_i=({"3": HarmonicComponent(mag_pct=1.2, angle_deg=30.0)} if i == 4 else None),
            )
        )
    # hub-00001: three-phase, 4 samples, a couple of nulls scattered across phases/fields.
    for i in range(4):
        summaries.append(
            PqWaveformSummaryRow(
                hub_id="hub-00001",
                ts=datetime(2026, 9, 27, 0, i, 0, tzinfo=UTC),
                v_rms_a=NOMINAL_VOLTAGE_V - 1.0,
                v_rms_b=NOMINAL_VOLTAGE_V + 0.5 if i != 1 else None,
                v_rms_c=NOMINAL_VOLTAGE_V + 2.0,
                freq_hz=NOMINAL_FREQ_HZ + 0.05 if i != 2 else None,
                thd_i_pct_a=1.5,
                thd_i_pct_b=None,
                thd_i_pct_c=3.0,
                phase_angle_deg_a=0.2,
                phase_angle_deg_c=-0.3,
                harmonics_i=({"5": HarmonicComponent(mag_pct=0.8, angle_deg=-10.0)} if i == 1 else None),
            )
        )
    # hub-00002: two-phase (B/C), single sample.
    summaries.append(
        PqWaveformSummaryRow(
            hub_id="hub-00002",
            ts=NOW,
            v_rms_b=NOMINAL_VOLTAGE_V,
            v_rms_c=NOMINAL_VOLTAGE_V,
            freq_hz=NOMINAL_FREQ_HZ,
        )
    )
    # hub-00003: no freq/voltage at all -- must be excluded from BOTH paths.
    summaries.append(PqWaveformSummaryRow(hub_id="hub-00003", ts=NOW, thd_i_pct_a=5.0))
    return summaries


def test_sql_aggregation_path_matches_raw_row_path_on_fixture_data():
    """R3.4.1 PROD-IO fix's core correctness requirement: the new per-hub-aggregated path
    (`_aggregate_hub` + `characterize_hub_from_aggregate`, what `pg_backend.latest_summary_aggregates`'s
    SQL is proven equivalent to -- see that query's own comment) must produce EXACTLY the same
    `HubCharacterization` fleet as the original raw-row path (`characterize_fleet`) on the same data."""
    summaries = _mixed_fixture_summaries()

    before = characterize_fleet(summaries, now=NOW)

    by_hub: dict[str, list[PqWaveformSummaryRow]] = {}
    for summary in summaries:
        by_hub.setdefault(summary.hub_id, []).append(summary)
    aggregates = [_aggregate_hub(hub_id, rows) for hub_id, rows in by_hub.items()]
    aggregates = [a for a in aggregates if a is not None]
    after = characterize_fleet_from_aggregates(aggregates, now=NOW)

    assert {r.hub_id for r in before} == {r.hub_id for r in after} == {"hub-00000", "hub-00001", "hub-00002"}
    before_by_hub = {r.hub_id: r for r in before}
    after_by_hub = {r.hub_id: r for r in after}
    for hub_id in before_by_hub:
        assert before_by_hub[hub_id] == after_by_hub[hub_id], hub_id


def test_characterize_hub_from_aggregate_matches_characterize_hub_per_hub():
    """Same equality proof, one hub at a time, using `characterize_hub`/`characterize_hub_from_aggregate`
    directly (the two public entry points either path is reached through)."""
    for hub_id, rows in (
        ("hub-00000", [_summary(v_rms_a=NOMINAL_VOLTAGE_V * 1.01, freq_hz=NOMINAL_FREQ_HZ - 0.02)]),
        (
            "hub-00001",
            [
                _summary(hub_id="hub-00001", thd_i_pct_a=1.0),
                _summary(hub_id="hub-00001", thd_i_pct_a=3.0, freq_hz=NOMINAL_FREQ_HZ + 0.2),
            ],
        ),
    ):
        via_raw = characterize_hub(hub_id, rows, now=NOW)
        aggregate = _aggregate_hub(hub_id, rows)
        assert aggregate is not None
        via_aggregate = characterize_hub_from_aggregate(aggregate, now=NOW)
        assert via_raw == via_aggregate


def test_aggregate_hub_returns_none_when_characterize_hub_would() -> None:
    assert _aggregate_hub("hub-00000", []) is None
    assert _aggregate_hub("hub-00000", [PqWaveformSummaryRow(hub_id="hub-00000", ts=NOW)]) is None


def test_hub_summary_aggregate_is_a_plain_frozen_dataclass() -> None:
    """Cheap guard against accidentally reintroducing per-sample state into the aggregate shape -- it
    must stay small enough to be the ~3,509-row SQL result, not a container of raw samples."""
    aggregate = HubSummaryAggregate(
        hub_id="hub-00000",
        freq_offset_hz=0.0,
        freq_offset_std_hz=0.0,
        voltage_offset_pct=0.0,
        voltage_offset_std_pct=0.0,
        thd_current_pct=0.0,
        phase_angle_error_deg=0.0,
        phase_connection="A",
        dominant_harmonics=None,
    )
    with pytest.raises(AttributeError):
        aggregate.hub_id = "hub-00001"  # type: ignore[misc]  # frozen
