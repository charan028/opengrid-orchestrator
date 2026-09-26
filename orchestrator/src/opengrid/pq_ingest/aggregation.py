"""Measured per-bank PQ aggregation (07-delivery/06 S6.5 step 3): the SAME S3.2
formulas (`opengrid.core.pq.aggregation`), now evaluated on live `PqWaveformSummaryRow`
readings instead of static `og.hub_inverter_pq` characterization -- never a second
implementation of imbalance/THD/offset math (BUILD.md S1).

Output feeds the three S6.5-step-4 consumers: the allocator's post-assignment
verification, the guardian's independent G-21..G-23, and the S5.4 continuous monitoring
loop -- all read `opengrid.core.pq.PqMeasurement` from here rather than re-deriving it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from opengrid.core.models.pq import PqWaveformSummaryRow
from opengrid.core.pq import PqMeasurement, per_phase_imbalance_pct

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
    thd_i_values = _all_phase_values(fresh, "thd_i_pct")
    imbalance_pct = per_phase_imbalance_pct(current_by_phase) if len(current_by_phase) >= 2 else 0.0

    return PqMeasurement(
        imbalance_pct=imbalance_pct,
        voltage_deviation_pct=_deviation_pct(voltage_values, NOMINAL_VOLTAGE_V),
        freq_deviation_hz=_deviation(freq_values, NOMINAL_FREQ_HZ),
        pf=_mean(pf_values) if pf_values else 1.0,
        thd_voltage_pct=_mean(thd_v_values),
        thd_current_pct=_mean(thd_i_values),
        current_a=sum(current_by_phase.values()) if current_by_phase else None,
    )


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
