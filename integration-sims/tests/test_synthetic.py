from datetime import UTC, datetime

from ogsim.market import synthetic


def test_price_is_deterministic_for_same_seed():
    ts = datetime(2026, 9, 25, 14, 30, tzinfo=UTC)
    p1 = synthetic.price_usd_per_mwh(ts, seed=42, hub="HB_HUBAVG")
    p2 = synthetic.price_usd_per_mwh(ts, seed=42, hub="HB_HUBAVG")
    assert p1 == p2
    assert p1 > 0


def test_price_varies_by_seed():
    ts = datetime(2026, 9, 25, 14, 30, tzinfo=UTC)
    p1 = synthetic.price_usd_per_mwh(ts, seed=1)
    p2 = synthetic.price_usd_per_mwh(ts, seed=2)
    assert p1 != p2


def test_load_positive_for_all_zones():
    ts = datetime(2026, 9, 25, 17, 0, tzinfo=UTC)
    for zone in synthetic.WEATHER_ZONES:
        assert synthetic.load_mw(ts, seed=1, zone=zone) > 0


def test_solar_is_zero_at_night():
    night = datetime(2026, 9, 25, 2, 0, tzinfo=UTC)
    actual, forecast = synthetic.solar_mw(night, seed=1)
    assert actual == 0.0
    assert forecast == 0.0


def test_solar_is_positive_at_midday():
    midday = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    actual, forecast = synthetic.solar_mw(midday, seed=1)
    assert actual > 0
    assert forecast > 0


def test_wind_is_always_nonnegative():
    for hour in range(0, 24, 3):
        ts = datetime(2026, 9, 25, hour, 0, tzinfo=UTC)
        actual, forecast = synthetic.wind_mw(ts, seed=7)
        assert actual >= 0
        assert forecast >= 0


def test_as_clearing_price_positive_for_all_services():
    ts = datetime(2026, 9, 25, 14, 0, tzinfo=UTC)
    for service in synthetic.AS_SERVICES:
        assert synthetic.as_clearing_price(ts, seed=1, service=service) > 0
