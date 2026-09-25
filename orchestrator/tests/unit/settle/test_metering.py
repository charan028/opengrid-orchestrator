"""TS-08-02 (interval metering matches raw telemetry) and the >=13-of-15 completeness rule
(02a S7.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from opengrid.settle.metering import meter_interval
from opengrid.settle.models import PowerSample


def _samples(kw_values: list[float], start: datetime) -> list[PowerSample]:
    return [
        PowerSample(hub_id="hub-1", ts=start + timedelta(minutes=i), kw=Decimal(str(kw)))
        for i, kw in enumerate(kw_values)
    ]


def test_full_15_of_15_samples_at_constant_4kw_yields_1_kwh_and_good_quality():
    """Hand computation: 15 one-minute samples at a constant 4 kW average to 4 kW; a 15-minute
    interval is 0.25 h, so delivered kWh = 4 kW * 0.25 h = 1.0 kWh exactly."""
    start = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
    samples = _samples([4.0] * 15, start)

    result = meter_interval(samples, interval_minutes=15, source="DIRECT_HUB_METER")

    assert result.delivered_kwh == Decimal("1.0")
    assert result.quality_flag == "GOOD"
    assert result.samples_present == 15
    assert result.samples_expected == 15


def test_13_of_15_samples_present_is_still_good():
    """13/15 = 0.8667 >= the 13-of-15 completeness floor -- still graded GOOD (02a S7.2)."""
    start = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
    samples = _samples([4.0] * 13, start)

    result = meter_interval(samples, interval_minutes=15, source="DIRECT_HUB_METER")

    assert result.quality_flag == "GOOD"
    # average is still 4 kW (only present samples average), so delivered kWh is unchanged.
    assert result.delivered_kwh == Decimal("1.0")


def test_below_13_of_15_samples_is_estimated():
    """Hand computation: 10 one-minute samples at 4 kW average to 4 kW; delivered kWh = 4 * 0.25 =
    1.0 kWh, but 10/15 = 0.667 < 13/15, so the interval is graded ESTIMATED, not GOOD."""
    start = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
    samples = _samples([4.0] * 10, start)

    result = meter_interval(samples, interval_minutes=15, source="DIRECT_HUB_METER")

    assert result.delivered_kwh == Decimal("1.0")
    assert result.quality_flag == "ESTIMATED"
    assert result.samples_present == 10


def test_zero_samples_is_zero_kwh_estimated():
    result = meter_interval([], interval_minutes=15, source="SCADA_OUTCOME")

    assert result.delivered_kwh == Decimal("0")
    assert result.quality_flag == "ESTIMATED"
    assert result.samples_present == 0


def test_varying_power_profile_averages_correctly():
    """Hand computation: samples 2,4,6,8 kW (4 of an expected 4) average to (2+4+6+8)/4 = 5 kW;
    over a 4-minute interval (1/15 h) delivered kWh = 5 * (4/60) = 0.333... kWh."""
    start = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
    samples = _samples([2.0, 4.0, 6.0, 8.0], start)

    result = meter_interval(samples, interval_minutes=4, source="AMI_INTERVAL")

    expected = Decimal("5") * (Decimal("4") / Decimal("60"))
    assert result.delivered_kwh == expected
    assert result.quality_flag == "GOOD"
