"""Interval metering from telemetry (02a S7.2): turns 1-minute power samples into a 15-minute
delivered-kWh figure per obligation, with the completeness/quality rule from the spec.

Pure function, no I/O -- `opengrid.settle.__init__.settle()` fetches the samples via the backend
and passes them in here.
"""

from __future__ import annotations

from decimal import Decimal

from opengrid.settle.models import (
    MIN_GOOD_SAMPLE_FRACTION,
    MeteringResult,
    MeterSource,
    PowerSample,
    QualityFlag,
)

_MINUTES_PER_HOUR = Decimal("60")


def meter_interval(
    samples: list[PowerSample],
    *,
    interval_minutes: int,
    source: MeterSource,
) -> MeteringResult:
    """Delivered kWh = average metered kW over the interval * interval duration in hours.

    Each `PowerSample` is a 1-minute-average kW reading, so kWh = kW * (1/60) per sample; averaging
    first and multiplying by the interval's duration is algebraically the same and lets us apply the
    >=13-of-15 completeness rule (02a S7.2) as one clean fraction:

    - `samples_present / samples_expected >= 13/15` -- gaps are filled by the interval's own average
      (a defensible interpolation for a flat-ish 1-minute cadence) and the interval is graded `GOOD`.
    - otherwise, the same average-based estimate is used but graded `ESTIMATED` (02a S7.2: "else
      interpolated, else ESTIMATED").
    - zero samples present -> zero delivered kWh, graded `ESTIMATED` (a full data gap, never `GOOD`).
    """
    samples_expected = interval_minutes
    samples_present = len(samples)
    duration_hours = Decimal(interval_minutes) / _MINUTES_PER_HOUR

    if samples_present == 0:
        return MeteringResult(
            delivered_kwh=Decimal("0"),
            quality_flag="ESTIMATED",
            source=source,
            samples_present=0,
            samples_expected=samples_expected,
        )

    average_kw = sum((s.kw for s in samples), Decimal("0")) / samples_present
    delivered_kwh = average_kw * duration_hours

    completeness = Decimal(samples_present) / Decimal(samples_expected)
    quality_flag: QualityFlag = "GOOD" if completeness >= MIN_GOOD_SAMPLE_FRACTION else "ESTIMATED"

    return MeteringResult(
        delivered_kwh=delivered_kwh,
        quality_flag=quality_flag,
        source=source,
        samples_present=samples_present,
        samples_expected=samples_expected,
    )
