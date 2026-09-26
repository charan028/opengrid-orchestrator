"""Live api.weather.gov hourly-forecast periods carry no `skyCover`; the converter must not reject them."""

from __future__ import annotations

from datetime import UTC, datetime

from opengrid.feeds.normalize import nws_forecast_to_feed_obs

_RECORDED_AT = datetime(2026, 9, 26, 4, 0, tzinfo=UTC)


def _payload(period: dict[str, object]) -> dict[str, object]:
    return {"properties": {"updateTime": "2026-09-26T03:00:00+00:00", "periods": [period]}}


def test_period_without_sky_cover_yields_temperature_and_dewpoint_only() -> None:
    period = {"startTime": "2026-09-25T23:00:00-05:00", "temperature": 88, "dewpoint": {"value": 21.1}}
    obs = nws_forecast_to_feed_obs(_payload(period), recorded_at=_RECORDED_AT)
    assert [o.series for o in obs] == ["temperature", "dewpoint"]


def test_period_with_sky_cover_still_yields_all_three() -> None:
    period = {
        "startTime": "2026-09-25T23:00:00-05:00",
        "temperature": 88,
        "dewpoint": {"value": 21.1},
        "skyCover": 20,
    }
    obs = nws_forecast_to_feed_obs(_payload(period), recorded_at=_RECORDED_AT)
    assert [o.series for o in obs] == ["temperature", "dewpoint", "sky_cover"]
