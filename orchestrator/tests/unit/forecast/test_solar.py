"""Pure math for D-24's solar-driven intraday price shape (`forecast/solar.py`): clear-sky shape,
evening-ramp weight, the two intensity-signal sources (ERCOT solar, NWS cloud cover) and their
preference order, and the shift applied on top of a quantile-persistence triple."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from opengrid.forecast.solar import (
    SolarShapeInput,
    apply_solar_shape,
    clear_sky_shape,
    evening_ramp_weight,
    resolve_solar_shape_input,
    solar_intensity_from_cloud_cover,
    solar_intensity_from_ercot,
)

# 2026-07-20 is a Monday; times chosen in UTC so `to_market_tz` (America/Chicago, UTC-5 in July) lands
# on round local hours: 05:00Z -> 00:00 local, 18:00Z -> 13:00 local (solar noon = midpoint of
# [SUNRISE_HOUR, SUNSET_HOUR] = [6.5, 19.5]), 00:30Z (next day) -> 19:30 local (== SUNSET_HOUR).
_MIDNIGHT_LOCAL = datetime(2026, 7, 20, 5, 0, tzinfo=UTC)
_NOON_LOCAL = datetime(2026, 7, 20, 18, 0, tzinfo=UTC)


def test_clear_sky_shape_zero_at_night_peak_at_noon() -> None:
    assert clear_sky_shape(_MIDNIGHT_LOCAL) == 0.0
    assert clear_sky_shape(_NOON_LOCAL) == pytest.approx(1.0, abs=1e-9)


def test_clear_sky_shape_between_zero_and_one_at_sunrise_ish() -> None:
    mid_morning = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)  # 09:00 local
    shape = clear_sky_shape(mid_morning)
    assert 0.0 < shape < 1.0


def test_evening_ramp_weight_peaks_right_after_sunset_then_decays() -> None:
    exact_sunset = datetime(2026, 7, 21, 0, 30, tzinfo=UTC)  # 19:30 local == SUNSET_HOUR
    assert evening_ramp_weight(exact_sunset) == pytest.approx(1.0, abs=1e-6)
    one_hour_after = datetime(2026, 7, 21, 1, 30, tzinfo=UTC)  # 20:30 local
    later_weight = evening_ramp_weight(one_hour_after)
    assert 0.0 < later_weight < 1.0
    assert later_weight < evening_ramp_weight(exact_sunset)
    assert evening_ramp_weight(_NOON_LOCAL) == 0.0  # daytime: no ramp yet


def test_evening_ramp_weight_zero_well_after_the_ramp_window() -> None:
    long_after = datetime(2026, 7, 21, 6, 0, tzinfo=UTC)  # 01:00 local, next day
    assert evening_ramp_weight(long_after) == 0.0


def test_solar_intensity_from_ercot_scales_to_that_days_own_peak() -> None:
    solar_mw = {
        _MIDNIGHT_LOCAL: 0.0,
        _NOON_LOCAL: 800.0,  # that day's peak
    }
    assert solar_intensity_from_ercot(solar_mw, _NOON_LOCAL) == pytest.approx(1.0)
    assert solar_intensity_from_ercot(solar_mw, _MIDNIGHT_LOCAL) == pytest.approx(0.0)


def test_solar_intensity_from_ercot_none_when_no_data_that_day() -> None:
    solar_mw = {_NOON_LOCAL: 800.0}
    other_day = datetime(2026, 8, 1, 17, 0, tzinfo=UTC)
    assert solar_intensity_from_ercot(solar_mw, other_day) is None


def test_solar_intensity_from_ercot_none_when_target_instant_missing() -> None:
    """The day has data (so a peak exists), but not at this exact target instant -- still `None`, not a
    guess, so the caller can fall back to NWS cloud cover instead of a fabricated ERCOT figure."""
    solar_mw = {_NOON_LOCAL: 800.0}
    fifteen_min_later = datetime(2026, 7, 20, 17, 15, tzinfo=UTC)
    assert solar_intensity_from_ercot(solar_mw, fifteen_min_later) is None


def test_solar_intensity_from_cloud_cover_reduces_clear_sky_shape() -> None:
    clear = solar_intensity_from_cloud_cover(_NOON_LOCAL, 0.0)
    overcast = solar_intensity_from_cloud_cover(_NOON_LOCAL, 100.0)
    partly = solar_intensity_from_cloud_cover(_NOON_LOCAL, 50.0)
    assert clear == pytest.approx(1.0, abs=1e-9)
    assert overcast == pytest.approx(0.0, abs=1e-9)
    assert 0.0 < partly < clear


def test_solar_intensity_from_cloud_cover_none_treated_as_clear() -> None:
    assert solar_intensity_from_cloud_cover(_NOON_LOCAL, None) == pytest.approx(clear_sky_shape(_NOON_LOCAL))


def test_resolve_prefers_ercot_over_cloud_cover() -> None:
    solar_mw = {_NOON_LOCAL: 500.0}
    shape = resolve_solar_shape_input(_NOON_LOCAL, solar_mw_by_ts=solar_mw, cloud_cover_pct=90.0)
    assert shape is not None
    assert shape.source == "ercot"
    assert shape.intensity == pytest.approx(1.0)  # ERCOT's own value, not the heavily-clouded proxy


def test_resolve_falls_back_to_cloud_cover_when_ercot_missing_that_day() -> None:
    shape = resolve_solar_shape_input(_NOON_LOCAL, solar_mw_by_ts=None, cloud_cover_pct=20.0)
    assert shape is not None
    assert shape.source == "nws_cloud_cover"


def test_resolve_returns_none_without_any_signal() -> None:
    assert resolve_solar_shape_input(_NOON_LOCAL, solar_mw_by_ts=None, cloud_cover_pct=None) is None


def test_apply_solar_shape_identity_when_no_signal() -> None:
    quantiles = (10.0, 20.0, 30.0)
    assert apply_solar_shape(quantiles, None) == quantiles


def test_apply_solar_shape_pulls_band_down_at_midday_intensity() -> None:
    quantiles = (10.0, 20.0, 30.0)
    shape = SolarShapeInput(intensity=1.0, ramp_weight=0.0, source="ercot")
    p10, p50, p90 = apply_solar_shape(quantiles, shape)
    assert p50 < 20.0
    assert p10 < 10.0
    assert p90 < 30.0
    assert p10 <= p50 <= p90


def test_apply_solar_shape_pushes_band_up_during_evening_ramp() -> None:
    quantiles = (10.0, 20.0, 30.0)
    shape = SolarShapeInput(intensity=0.0, ramp_weight=1.0, source="ercot")
    p10, p50, p90 = apply_solar_shape(quantiles, shape)
    assert p50 > 20.0
    assert p10 <= p50 <= p90


def test_apply_solar_shape_preserves_ordering_on_zero_width_band() -> None:
    """A zero-width point estimate (the diurnal fallback's degenerate case) must not blow up or invert
    ordering -- the shift is proportional to band width, so it is simply zero here."""
    quantiles = (15.0, 15.0, 15.0)
    shape = SolarShapeInput(intensity=1.0, ramp_weight=0.0, source="ercot")
    assert apply_solar_shape(quantiles, shape) == quantiles
