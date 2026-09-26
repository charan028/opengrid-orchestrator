"""Measured per-bank PQ aggregation (07-delivery/06 S6.5 step 3): the SAME S3.2
formulas (`opengrid.core.pq.aggregation`), now evaluated on live `PqWaveformSummaryRow`
readings instead of static `og.hub_inverter_pq` characterization -- never a second
implementation of imbalance/THD/offset math (BUILD.md S1).

Output feeds the three S6.5-step-4 consumers: the allocator's post-assignment
verification, the guardian's independent G-21..G-23, and the S5.4 continuous monitoring
loop -- all read `opengrid.core.pq.PqMeasurement` from here rather than re-deriving it.
"""

from __future__ import annotations

import cmath
import math
from collections.abc import Sequence
from datetime import datetime

from opengrid.core.models.pq import PqWaveformSummaryRow
from opengrid.core.pq import PqMeasurement, bank_thd_current_pct, per_phase_imbalance_pct

# Split-phase US residential leg-to-neutral nominal (S2's ANSI C84.1 Range A default is
# +/-% of this); used only to express a deviation, never a live-system constant.
NOMINAL_VOLTAGE_V = 240.0
NOMINAL_FREQ_HZ = 60.0
_PHASES = ("a", "b", "c")


def fresh_summaries(
    summaries: Sequence[PqWaveformSummaryRow], now: datetime, freshness_s: float
) -> list[PqWaveformSummaryRow]:
    """K1's "stale -> exclude" pattern (S5.4: a summary older than 2x the profile's
    telemetry cadence is treated as missing, never as compliant): filters `summaries` to
    those no older than `freshness_s` relative to `now`."""
    return [s for s in summaries if (now - s.ts).total_seconds() <= freshness_s]


def bank_measurement(
    summaries: Sequence[PqWaveformSummaryRow], now: datetime, freshness_s: float
) -> PqMeasurement | None:
    """S6.5 step 3: aggregates fresh hub summaries on one bank into one `PqMeasurement`
    for `opengrid.core.pq.evaluate_envelope`/`step_hysteresis`. Returns `None` if no
    fresh summary carries both a frequency and a voltage reading (K1: missing data is
    never silently treated as compliant)."""
    fresh = fresh_summaries(summaries, now, freshness_s)
    if not fresh:
        return None

    current_by_phase = _sum_by_phase(fresh, "i_rms")
    voltage_values = _all_phase_values(fresh, "v_rms")
    freq_values = [float(s.freq_hz) for s in fresh if s.freq_hz is not None]
    if not voltage_values or not freq_values:
        return None

    pf_values = _all_phase_values(fresh, "pf")
    thd_v_values = _all_phase_values(fresh, "thd_v_pct")
    imbalance_pct = per_phase_imbalance_pct(current_by_phase) if len(current_by_phase) >= 2 else 0.0

    return PqMeasurement(
        imbalance_pct=imbalance_pct,
        voltage_deviation_pct=_deviation_pct(voltage_values, NOMINAL_VOLTAGE_V),
        freq_deviation_hz=_deviation(freq_values, NOMINAL_FREQ_HZ),
        pf=_mean(pf_values) if pf_values else 1.0,
        thd_voltage_pct=_mean(thd_v_values),
        thd_current_pct=_bank_thd_current_pct(fresh),
        current_a=sum(current_by_phase.values()) if current_by_phase else None,
    )


def _hub_fundamental_current_a(summary: PqWaveformSummaryRow) -> float | None:
    """This hub's own fundamental current base (S3.2(b) needs one per hub): the sum of
    whichever `i_rms_<phase>` fields this summary reports (RMS, not the pure fundamental
    bin, but harmonic content is a small perturbation on it -- the same approximation
    `opengrid.guardian.pq_repo`'s modelled fallback documents). `None` (never 0.0) when the
    summary carries no current reading at all, so this hub is excluded from the bank THD_I
    aggregate rather than silently diluting it (K1's "missing -> excluded" pattern)."""
    values = [float(v) for phase in _PHASES if (v := getattr(summary, f"i_rms_{phase}")) is not None]
    return sum(values) if values else None


def _hub_thd_i_pct(summary: PqWaveformSummaryRow) -> float | None:
    """This hub's own aggregate THD_I% across whichever phases it reports this summary --
    used only for the conservative no-phase-data fallback below, never for the vector-sum
    path (which reads `harmonics_i` directly)."""
    values = [float(v) for phase in _PHASES if (v := getattr(summary, f"thd_i_pct_{phase}")) is not None]
    return _mean(values) if values else None


def _group_latest_by_hub(summaries: Sequence[PqWaveformSummaryRow]) -> dict[str, PqWaveformSummaryRow]:
    """One representative reading per hub (the freshest in the window) for the per-hub
    current/harmonics S3.2(b) needs -- a hub with more than one summary in the freshness
    window must count once toward the bank's fundamental-current base, not once per
    summary (which would double-weight it against a hub with only one reading this cycle)."""
    latest: dict[str, PqWaveformSummaryRow] = {}
    for summary in summaries:
        current = latest.get(summary.hub_id)
        if current is None or summary.ts > current.ts:
            latest[summary.hub_id] = summary
    return latest


def _harmonic_phasor_a(mag_pct: float, angle_deg: float, fundamental_a: float) -> complex:
    """One harmonic order's phasor in absolute Amps (magnitude % of THIS hub's own
    fundamental, S6.4a) and angle relative to the shared sync reference (S6.4a), converted
    to the complex-current form `opengrid.core.pq.harmonic_vector_sum` vector-sums."""
    return cmath.rect(fundamental_a * mag_pct / 100.0, math.radians(angle_deg))


def _bank_thd_current_pct(fresh: Sequence[PqWaveformSummaryRow]) -> float:
    """S3.2(b), fixed (TS-15b/PR #16 cross-system defect): the bank's THD_I is the harmonic
    VECTOR sum across hubs, never the arithmetic MEAN of their individual THD_I% readings --
    a mean systematically overstates the true (partially cancelling) aggregate whenever
    hubs' harmonic phases are not perfectly aligned (the reported defect: 2.12% reported vs.
    0.18% true at seed 15), and can never fall below the true value the way a mean can rise
    above it.

    Hubs whose CONCURRENT summary carries a `harmonics_i` block (S6.4b's harmonic-detail
    sub-block, published less often than the fast sub-block -- most summaries in a window
    will not have one) are vector-summed order-by-order via the canonical
    `opengrid.core.pq.bank_thd_current_pct` (never re-derived here). A hub with current data
    but NO harmonics phase this cycle falls back to S4.a's documented conservative rule --
    "assumes worst-case phase alignment when dominant_harmonics phase data is stale or
    missing" -- by adding its own scalar harmonic-current magnitude (`thd_i_pct/100 *
    fundamental_a`) directly onto the vector-summed hubs' total (linear/stacking addition,
    §3.2(b)'s own worst-case formula), rather than either dropping it or vector-summing it
    with an assumed phase it does not have. A hub with no current reading at all is excluded
    entirely (K1), never treated as contributing zero."""
    by_hub = _group_latest_by_hub(fresh)

    vector_hub_harmonics: list[dict[int, complex]] = []
    vector_hub_fundamental_a: list[float] = []
    fallback_harmonic_current_a = 0.0
    fallback_fundamental_a = 0.0

    for summary in by_hub.values():
        fundamental_a = _hub_fundamental_current_a(summary)
        if fundamental_a is None or fundamental_a <= 0.0:
            continue
        if summary.harmonics_i:
            vector_hub_harmonics.append(
                {
                    int(order): _harmonic_phasor_a(float(c.mag_pct), float(c.angle_deg), fundamental_a)
                    for order, c in summary.harmonics_i.items()
                }
            )
            vector_hub_fundamental_a.append(fundamental_a)
            continue
        thd_i_pct = _hub_thd_i_pct(summary)
        if thd_i_pct is None:
            continue
        fallback_harmonic_current_a += fundamental_a * thd_i_pct / 100.0
        fallback_fundamental_a += fundamental_a

    vector_harmonic_current_a = 0.0
    if vector_hub_harmonics:
        vector_pct = bank_thd_current_pct(vector_hub_harmonics, vector_hub_fundamental_a)
        vector_harmonic_current_a = vector_pct / 100.0 * sum(vector_hub_fundamental_a)

    bank_fundamental_a = sum(vector_hub_fundamental_a) + fallback_fundamental_a
    if bank_fundamental_a <= 0.0:
        return 0.0
    total_harmonic_current_a = vector_harmonic_current_a + fallback_harmonic_current_a
    return total_harmonic_current_a / bank_fundamental_a * 100.0


def _sum_by_phase(summaries: Sequence[PqWaveformSummaryRow], prefix: str) -> dict[str, float]:
    """Sums a `<prefix>_<phase>` field across every hub summary, per phase -- S3.2(c)'s
    per-phase TOTAL current a bank's imbalance is computed from."""
    totals: dict[str, float] = {}
    for phase in _PHASES:
        values = [float(v) for s in summaries if (v := getattr(s, f"{prefix}_{phase}")) is not None]
        if values:
            totals[phase.upper()] = sum(values)
    return totals


def _all_phase_values(summaries: Sequence[PqWaveformSummaryRow], prefix: str) -> list[float]:
    """Flattens a `<prefix>_<phase>` field across every hub summary AND every phase --
    used for quantities (voltage, PF, THD) where the bank-level figure is a simple mean
    across whichever phases reported a value this cycle."""
    values: list[float] = []
    for phase in _PHASES:
        values.extend(float(v) for s in summaries if (v := getattr(s, f"{prefix}_{phase}")) is not None)
    return values


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _deviation(values: Sequence[float], nominal: float) -> float:
    return abs(_mean(values) - nominal) if values else 0.0


def _deviation_pct(values: Sequence[float], nominal: float) -> float:
    if not values or nominal <= 0.0:
        return 0.0
    return abs((_mean(values) - nominal) / nominal) * 100.0
