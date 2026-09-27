"""Derives `og.hub_inverter_pq` characterization from measured `PqWaveformSummaryRow` history
(07-delivery/06 S3.1/S5.1/S6.5). Pure aggregation -- no I/O (BUILD.md S5a); the caller
(`opengrid.pq_ingest.run_characterization_pass`) fetches summaries and persists the result.

**Blocker fixed (post-deploy report): `og.hub_inverter_pq` had 0 live rows.** Nothing in the
build populated it -- waveform summaries were being ingested (S6.5 step 1) but never
aggregated into the per-inverter characterization row S5.1's quality score, the allocator's
S5.2 eligibility filter, and the assets module's drift sweep (`opengrid.assets`) all read.
This module is that missing aggregation step: S3.1's per-inverter model (frequency/voltage
offset and spread, THD, dominant harmonics, phase-angle error), recomputed periodically "from
telemetry, not by the allocator per cycle" (S5.1) -- i.e. a periodic characterization PASS,
never derived inline on the hot ingest path.

One field this module cannot derive from waveform telemetry alone: `kva_rating` (a nameplate
rating, not an electrical-quality measurement). `DEFAULT_KVA_RATING` is a documented,
conservative stand-in (Base's confirmed 11 kW/unit, memory `base-battery-specs`, divided by an
assumed 0.95 PF) -- flagged in the build report as a gap for whichever module later seeds real
per-hub nameplate data (a natural `og.hub`-driven follow-up, out of this package's scope).
`pf_min_leading`/`pf_min_lagging`/`response_time_ms`/`ride_through_class`/`asset_state*` are
never touched by this module's upsert (see `pg_backend.upsert_hub_inverter_pq_batch`'s own
`ON CONFLICT` column list) -- they keep whatever the row already has (the table's own DEFAULTs
on first insert), so a later, better-informed writer of those fields is never clobbered by a
characterization refresh.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import pstdev

from opengrid.core.models.pq import Harmonics, PhaseConnection, PqWaveformSummaryRow
from opengrid.core.pq import inverter_quality_score

NOMINAL_FREQ_HZ = 60.0
NOMINAL_VOLTAGE_V = 240.0
# Nameplate kVA is not observable from waveform telemetry (see module docstring) -- Base's
# confirmed 11 kW/unit (memory: base-battery-specs) at an assumed 0.95 PF, as a conservative
# stand-in until real per-hub nameplate data is available.
ASSUMED_PF_FOR_KVA_RATING = 0.95
DEFAULT_KVA_RATING = 11.0 / ASSUMED_PF_FOR_KVA_RATING

_PHASES = ("a", "b", "c")
_CANONICAL_PAIRS = {frozenset({"A", "B"}): "AB", frozenset({"B", "C"}): "BC", frozenset({"A", "C"}): "CA"}


@dataclass(frozen=True, slots=True)
class HubCharacterization:
    """One `og.hub_inverter_pq` characterization -- the row shape
    `pg_backend.upsert_hub_inverter_pq_batch` persists."""

    hub_id: str
    phase_connection: PhaseConnection
    kva_rating: float
    freq_offset_hz: float
    freq_offset_std_hz: float
    voltage_offset_pct: float
    voltage_offset_std_pct: float
    thd_current_pct: float
    dominant_harmonics: dict[str, dict[str, float]] | None
    phase_angle_error_deg: float
    quality_score: float
    last_estimated_at: datetime


def _infer_phase_connection(summaries: Sequence[PqWaveformSummaryRow]) -> PhaseConnection:
    """Which leg(s) this hub reports on, from which `v_rms_<phase>` fields are ever populated
    across the window -- real measured data (S3.1's `phase_connection`), not a guess."""
    present = {
        phase.upper() for phase in _PHASES for s in summaries if getattr(s, f"v_rms_{phase}") is not None
    }
    if len(present) >= 3:
        return "ABC"
    if len(present) == 2:
        return _CANONICAL_PAIRS.get(frozenset(present), "AB")  # type: ignore[return-value]
    if len(present) == 1:
        (only,) = present
        return only  # type: ignore[return-value]
    return "A"  # no per-phase voltage in this window at all -- degrade to a safe default


def _dominant_harmonics(summaries: Sequence[PqWaveformSummaryRow]) -> Harmonics | None:
    """The most recent non-null `harmonics_i` block in the window -- the harmonic-detail
    sub-block only publishes periodically/on-change (S6.4b), so most summaries in a window
    carry none; using the latest ONE that does is more representative than averaging stale
    detail blocks together."""
    for summary in sorted(summaries, key=lambda s: s.ts, reverse=True):
        if summary.harmonics_i:
            return summary.harmonics_i
    return None


def _phase_connection_from_presence(has_a: bool, has_b: bool, has_c: bool) -> PhaseConnection:
    """Same ladder as `_infer_phase_connection`, but from three "was this phase EVER reported in the
    window" booleans instead of scanning raw rows -- `HubSummaryAggregate`'s SQL-computed input
    (R3.4.1 PROD-IO fix, see that dataclass's docstring)."""
    present = {phase for phase, has in (("A", has_a), ("B", has_b), ("C", has_c)) if has}
    if len(present) >= 3:
        return "ABC"
    if len(present) == 2:
        return _CANONICAL_PAIRS.get(frozenset(present), "AB")  # type: ignore[return-value]
    if len(present) == 1:
        (only,) = present
        return only  # type: ignore[return-value]
    return "A"  # no per-phase voltage in this window at all -- degrade to a safe default


@dataclass(frozen=True, slots=True)
class HubSummaryAggregate:
    """Per-hub aggregate of a characterization window's `PqWaveformSummaryRow`s -- every field here is
    exactly what `_aggregate_hub` computes FROM the raw per-sample rows in Python, so
    `characterize_hub_from_aggregate` can assemble the identical `HubCharacterization` from either this
    OR the raw rows.

    R3.4.1 PROD-IO fix: production's `run_characterization_pass` used to fetch ~101,761 raw sample rows
    (15 min window x ~3,509 hubs) and run this aggregation in Python on the event loop thread every
    5 minutes -- 4-5s of loop-blocking work (heartbeat pool timeouts, inflated allocator/stuck_selected
    phases). `pg_backend.PgPqIngestBackend.latest_summary_aggregates` computes this SAME aggregation IN
    SQL instead, returning ~1 row/hub (~3,509 rows, not ~100k)."""

    hub_id: str
    freq_offset_hz: float
    freq_offset_std_hz: float
    voltage_offset_pct: float
    voltage_offset_std_pct: float
    thd_current_pct: float
    phase_angle_error_deg: float
    phase_connection: PhaseConnection
    dominant_harmonics: Harmonics | None


def _aggregate_hub(hub_id: str, summaries: Sequence[PqWaveformSummaryRow]) -> HubSummaryAggregate | None:
    """The pure-Python reference aggregation `pg_backend.latest_summary_aggregates`'s SQL must match
    (proven by `tests/unit/pq_ingest/test_characterize.py`'s before/after equality test) -- extracted
    from what was `characterize_hub`'s inline body before the R3.4.1 fix, unchanged. Returns `None` on
    the same "not enough data yet" condition `characterize_hub` always has (K1: never guess)."""
    if not summaries:
        return None

    freq_values = [float(s.freq_hz) for s in summaries if s.freq_hz is not None]
    voltage_devs: list[float] = []
    thd_values: list[float] = []
    phase_angles: list[float] = []
    phase_present = {phase: False for phase in _PHASES}
    for phase in _PHASES:
        voltage_devs.extend(
            (float(v) - NOMINAL_VOLTAGE_V) / NOMINAL_VOLTAGE_V * 100.0
            for s in summaries
            if (v := getattr(s, f"v_rms_{phase}")) is not None
        )
        phase_present[phase] = any(getattr(s, f"v_rms_{phase}") is not None for s in summaries)
        thd_values.extend(float(v) for s in summaries if (v := getattr(s, f"thd_i_pct_{phase}")) is not None)
        phase_angles.extend(
            float(v) for s in summaries if (v := getattr(s, f"phase_angle_deg_{phase}")) is not None
        )

    if not freq_values or not voltage_devs:
        return None

    return HubSummaryAggregate(
        hub_id=hub_id,
        freq_offset_hz=sum(freq_values) / len(freq_values) - NOMINAL_FREQ_HZ,
        freq_offset_std_hz=pstdev(freq_values) if len(freq_values) > 1 else 0.0,
        voltage_offset_pct=abs(sum(voltage_devs) / len(voltage_devs)),
        voltage_offset_std_pct=pstdev(voltage_devs) if len(voltage_devs) > 1 else 0.0,
        thd_current_pct=sum(thd_values) / len(thd_values) if thd_values else 0.0,
        phase_angle_error_deg=sum(phase_angles) / len(phase_angles) if phase_angles else 0.0,
        phase_connection=_phase_connection_from_presence(
            phase_present["a"], phase_present["b"], phase_present["c"]
        ),
        dominant_harmonics=_dominant_harmonics(summaries),
    )


def characterize_hub_from_aggregate(aggregate: HubSummaryAggregate, *, now: datetime) -> HubCharacterization:
    """Assembles ONE hub's `HubCharacterization` from an already-aggregated `HubSummaryAggregate`
    (R3.4.1 fix) -- the exact same final-assembly step `characterize_hub` does, just skipping the
    per-sample statistics (already computed, whether by `_aggregate_hub` in Python or
    `pg_backend.latest_summary_aggregates` in SQL)."""
    quality_score = inverter_quality_score(
        aggregate.freq_offset_hz,
        aggregate.voltage_offset_pct,
        aggregate.thd_current_pct,
        aggregate.phase_angle_error_deg,
    )
    harmonics = aggregate.dominant_harmonics
    return HubCharacterization(
        hub_id=aggregate.hub_id,
        phase_connection=aggregate.phase_connection,
        kva_rating=DEFAULT_KVA_RATING,
        freq_offset_hz=aggregate.freq_offset_hz,
        freq_offset_std_hz=aggregate.freq_offset_std_hz,
        voltage_offset_pct=aggregate.voltage_offset_pct,
        voltage_offset_std_pct=aggregate.voltage_offset_std_pct,
        thd_current_pct=aggregate.thd_current_pct,
        dominant_harmonics=(
            {
                order: {"mag_pct": float(c.mag_pct), "angle_deg": float(c.angle_deg)}
                for order, c in harmonics.items()
            }
            if harmonics
            else None
        ),
        phase_angle_error_deg=aggregate.phase_angle_error_deg,
        quality_score=quality_score,
        last_estimated_at=now,
    )


def characterize_hub(
    hub_id: str, summaries: Sequence[PqWaveformSummaryRow], *, now: datetime
) -> HubCharacterization | None:
    """S3.1/S5.1: aggregates a rolling window of `summaries` for ONE hub into its
    characterization. Returns `None` if there is not enough measured data yet (K1: never
    guess a characterization from an empty or frequency/voltage-less window).

    Kept for callers that only have raw rows (e.g. tests proving `pg_backend`'s SQL aggregation matches
    this Python path exactly); `run_characterization_pass`'s production path now goes through
    `characterize_hub_from_aggregate` + SQL-side aggregation instead (R3.4.1 fix, see
    `HubSummaryAggregate`'s docstring)."""
    aggregate = _aggregate_hub(hub_id, summaries)
    if aggregate is None:
        return None
    return characterize_hub_from_aggregate(aggregate, now=now)


def characterize_fleet(
    summaries: Sequence[PqWaveformSummaryRow], *, now: datetime
) -> list[HubCharacterization]:
    """Groups `summaries` (already fetched in ONE batched read for every hub of interest, see
    `opengrid.pq_ingest.run_characterization_pass`) by `hub_id` and characterizes each --
    never one query/characterization per hub."""
    by_hub: dict[str, list[PqWaveformSummaryRow]] = {}
    for summary in summaries:
        by_hub.setdefault(summary.hub_id, []).append(summary)
    results = (characterize_hub(hub_id, rows, now=now) for hub_id, rows in by_hub.items())
    return [r for r in results if r is not None]


def characterize_fleet_from_aggregates(
    aggregates: Sequence[HubSummaryAggregate], *, now: datetime
) -> list[HubCharacterization]:
    """R3.4.1 fix: the production path's counterpart to `characterize_fleet`, over already-aggregated
    rows (`pg_backend.latest_summary_aggregates`) instead of ~100k raw samples. Pure and cheap enough
    (one dataclass assembly per hub, ~3,509 of them) to run via `asyncio.to_thread` from
    `run_characterization_pass` without meaningfully delaying the thread-pool."""
    return [characterize_hub_from_aggregate(aggregate, now=now) for aggregate in aggregates]
